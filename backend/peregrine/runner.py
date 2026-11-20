"""Runs one trtllm-serve process at a time (TRT-LLM serves one model per process) and tracks its state."""
import asyncio
import collections
import os
import signal
import time

import httpx
import yaml

from . import DATA, ENGINE_PORT, LOGS, VENV_BIN, hub

FATAL = "Error in event loop"  # TRT-LLM 1.3.0 py_executor's log line when the executor thread dies


class Engine:
    def __init__(self):
        self.state = "stopped"  # stopped | starting | ready | error
        self.model: dict | None = None
        self.context: int | None = None
        self.error: str | None = None
        self.since = time.time()
        self.proc: asyncio.subprocess.Process | None = None
        self.baseline_used_mib: int | None = None  # VRAM other apps held right before we loaded
        self.log = collections.deque(maxlen=400)
        self.stats = collections.deque(maxlen=50)
        self.ready = asyncio.Event()  # set whenever state leaves "starting"
        self.lock = asyncio.Lock()
        self.pending: str | None = None  # model id a load() call is working on

    def used_mib(self, g: dict) -> int:
        """VRAM held by everything except our model, for fit math."""
        return self.baseline_used_mib if self.model and self.baseline_used_mib is not None else g["used_mib"]

    def status(self) -> dict:
        m = self.model or {}
        return {"state": self.state, "model": m.get("id"), "repo": m.get("repo"), "context": self.context,
                "error": self.error, "since": self.since, "log_tail": list(self.log)[-3:],
                "last_request": self.stats[-1] if self.stats else None}

    async def load(self, model_id: str, context: int | None = None) -> None:
        self.pending = model_id
        try:
            await self._load(model_id, context)
        finally:
            if self.pending == model_id:
                self.pending = None

    async def _load(self, model_id: str, context: int | None) -> None:
        async with self.lock:
            m = hub.local_model(model_id)  # KeyError if not downloaded
            had_proc = self.proc is not None
            await self._stop()
            if had_proc:
                await asyncio.sleep(2)  # let the driver hand the old model's VRAM back before measuring
            g = await asyncio.to_thread(hub.gpu)
            f = hub.local_fit(m, hub.budget(g))
            ctx = context or f["default_context"]
            if ctx > f["max_context"] or ctx < hub.MIN_CONTEXT:
                raise ValueError(f"{m['id']} needs more GPU memory than is free: it fits {f['max_context']} tokens "
                                 f"of context, {ctx} were asked for. Close other GPU apps or pick a smaller model.")
            launch = m["launch"]
            cfg = {
                "max_batch_size": 4,  # one chat plus a few parallel agent calls
                "max_num_tokens": 8192,
                "enable_chunked_prefill": True,  # without it, prompts over max_num_tokens are rejected
                "kv_cache_config": {"max_tokens": ctx, "free_gpu_memory_fraction": 0.9, "enable_block_reuse": True},
                "cuda_graph_config": {"batch_sizes": [1, 2, 4], "enable_padding": True},  # default captures 34 graphs
                "guided_decoding_backend": "xgrammar",  # otherwise response_format is silently ignored
            }
            if launch["kv_cache_dtype"] == "fp8":
                cfg["kv_cache_config"]["dtype"] = "fp8"
            cfg_path = DATA / "engine.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg))
            cmd = [str(VENV_BIN / "trtllm-serve"), m["path"], "--host", "127.0.0.1", "--port", str(ENGINE_PORT),
                   "--served_model_name", m["id"], "--max_seq_len", str(ctx), "--config", str(cfg_path),
                   "--generation-config", "auto", "--no-telemetry"]
            if launch.get("tool_parser"):
                cmd += ["--tool_parser", launch["tool_parser"]]
            if launch.get("reasoning_parser"):
                cmd += ["--reasoning_parser", launch["reasoning_parser"]]
            self.log.clear()
            self.log.append("$ " + " ".join(cmd))
            env = {**os.environ, "HF_HUB_OFFLINE": "1"}
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
                start_new_session=True)  # own process group, so stop() also reaches the MPI workers
            self.state, self.model, self.context, self.error = "starting", m, ctx, None
            self.baseline_used_mib, self.since, self.ready = g["used_mib"], time.time(), asyncio.Event()
            asyncio.create_task(self._watch(self.proc))

    async def unload(self) -> None:
        async with self.lock:
            await self._stop()

    async def ensure(self, name: str | None) -> dict:
        """Make the requested model ready, loading it on demand like LM Studio's JIT mode."""
        if name and name != self.pending and (not self.model or name != self.model["id"]):
            try:
                hub.local_model(name)
            except KeyError:
                raise LookupError(name) from None
            await self.load(name)
        elif self.pending:
            async with self.lock:  # another request is starting this model; wait for the process to launch
                pass
        if not self.model:
            raise LookupError(name)
        await self.ready.wait()
        m = self.model  # re-read: an unload or a switch can land while we waited
        if self.state != "ready" or not m:
            raise RuntimeError(self.error or f"{name or 'The model'} was unloaded while this request waited.")
        if name and m["id"] != name:
            raise RuntimeError(f"{name} was swapped for {m['id']} while this request waited. Send it again.")
        return m

    def record(self, t0, first, last, tokens, prompt_tokens) -> None:
        self.stats.append({
            "at": time.time(), "prompt_tokens": prompt_tokens, "completion_tokens": tokens,
            "ttft_ms": round((first - t0) * 1000) if first else None,
            "tokens_per_s": round((tokens - 1) / (last - first), 1) if tokens and tokens > 1 and first and last > first else None,
        })

    async def _watch(self, proc) -> None:
        pump = asyncio.create_task(self._pump(proc))
        async with httpx.AsyncClient() as c:
            while proc.returncode is None and self.proc is proc and self.state == "starting":
                try:
                    if (await c.get(f"http://127.0.0.1:{ENGINE_PORT}/health", timeout=5)).status_code == 200:
                        self.state, self.since = "ready", time.time()
                        self.ready.set()
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        await proc.wait()
        await pump
        if self.proc is proc:  # died on its own or was killed by _pump, not through _stop()
            self.proc, self.state, self.since = None, "error", time.time()
            tail = "\n".join(x for x in list(self.log)[-40:] if "Error" in x or "error" in x)[-1500:]
            self.error = f"trtllm-serve exited with code {proc.returncode}.\n{tail}"
            self.ready.set()

    async def _pump(self, proc) -> None:
        """Copy output to the ring buffer and engine.log. Splits on \\r too: progress bars never print \\n."""
        LOGS.mkdir(parents=True, exist_ok=True)
        buf = ""
        with open(LOGS / "engine.log", "a", buffering=1) as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {self.log[0] if self.log else ''}\n")
            while chunk := await proc.stdout.read(65536):
                buf += chunk.decode(errors="replace")
                *lines, buf = buf.replace("\r", "\n").split("\n")
                for line in filter(None, (x.rstrip() for x in lines)):
                    self.log.append(line)
                    f.write(line + "\n")
                    if FATAL in line:
                        # the executor thread is dead but the HTTP server (and /health) stays up and every
                        # request hangs; kill it so the state turns to error and waiting requests return
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    async def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 10)):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(proc.wait(), wait)
                    break
                except asyncio.TimeoutError:
                    continue
        self.state, self.model, self.context, self.error = "stopped", None, None, None
        self.baseline_used_mib, self.since = None, time.time()
        self.ready.set()  # wake anyone waiting on the old load

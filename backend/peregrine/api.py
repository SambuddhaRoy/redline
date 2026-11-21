"""HTTP API on 127.0.0.1:47800.

/v1/*      OpenAI-compatible, proxied to trtllm-serve. Hermes Agent and the chat UI talk to this.
/manage/*  everything the desktop UI needs: GPU, catalog, search, downloads, library, load/unload, Hermes.

Nothing lives under /api/v1/models, /api/tags or /props: Hermes probes those paths to detect LM Studio,
Ollama and llama.cpp, and must fall through to plain /v1/models.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import ENGINE_PORT, GATEWAY_PORT, HERMES_MIN_CONTEXT, __version__, hermes, hub
from .runner import Engine

engine = Engine()
downloads = hub.Downloads()
upstream = httpx.AsyncClient(base_url=f"http://127.0.0.1:{ENGINE_PORT}", timeout=httpx.Timeout(None, connect=10))


@asynccontextmanager
async def lifespan(_app):
    yield
    downloads.cancel_all()
    await engine.unload()


app = FastAPI(title="Peregrine", version=__version__, lifespan=lifespan)
# Only local callers by Host header: blocks DNS-rebinding pages from driving the API through the browser.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
# Browsers attach Origin; Hermes, SDKs and curl don't. Without this any web page could fire no-cors POSTs
# here (unload, model swaps, Hermes config rewrites). The desktop app's webview is the only other origin.
ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{GATEWAY_PORT}", f"http://localhost:{GATEWAY_PORT}",
    "http://tauri.localhost", "https://tauri.localhost", "tauri://localhost",  # Tauri 2 on Windows / others
    "http://localhost:1420", "http://127.0.0.1:1420",  # `tauri dev` (Vite)
}


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"error": {"message": "Cross-origin requests aren't allowed.", "code": 403}}, 403)
    return await call_next(request)


# Added last so it runs first: answers the app's preflights and stamps CORS headers on every reply.
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["*"], allow_headers=["*"])


# ---------- OpenAI-compatible ----------

# trtllm-serve rejects unknown request fields (extra="forbid"); Hermes and other clients send extras.
ALLOWED = {"model", "messages", "prompt", "tools", "tool_choice", "parallel_tool_calls", "stream", "stream_options",
           "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "min_p", "stop", "seed", "n",
           "presence_penalty", "frequency_penalty", "repetition_penalty", "logit_bias", "logprobs", "top_logprobs",
           "echo", "suffix", "best_of", "response_format", "user", "reasoning_effort", "chat_template_kwargs"}
EFFORT = {"minimal": "low", "xhigh": "high", "ultra": "max"}  # trtllm-serve takes low|medium|high|max|none


def sanitize(body: dict) -> dict:  # ponytail: allowlist; widen when a client needs a new field
    out = {k: v for k, v in body.items() if k in ALLOWED}
    if out.get("reasoning_effort") in EFFORT:
        out["reasoning_effort"] = EFFORT[out["reasoning_effort"]]
    return out


def oai_error(status: int, message: str) -> JSONResponse:
    kind = "invalid_request_error" if status < 500 else "server_error"
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}}, status)


@app.get("/v1/models")
def v1_models():
    g = hub.gpu()
    data = []
    for m in hub.library():
        loaded = engine.model and engine.model["id"] == m["id"]
        ctx = engine.context if loaded else hub.local_fit(m, hub.budget(g, engine.used_mib(g)))["default_context"]
        # context_length / max_model_len: Hermes reads the window from here; trtllm-serve's own card lacks it
        data.append({"id": m["id"], "object": "model", "created": int(m.get("downloaded_at", 0)),
                     "owned_by": m["repo"].split("/")[0], "context_length": ctx, "max_model_len": ctx,
                     "loaded": bool(loaded)})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await proxy(request, "/v1/chat/completions")


@app.post("/v1/completions")
async def completions(request: Request):
    return await proxy(request, "/v1/completions")


async def proxy(request: Request, path: str):
    try:
        body = await request.json()
    except ValueError:
        return oai_error(400, "Request body must be JSON.")
    if not isinstance(body, dict):
        return oai_error(400, "Request body must be a JSON object.")
    body = sanitize(body)
    try:
        model = await engine.ensure(body.get("model"))
    except LookupError:
        names = ", ".join(m["id"] for m in hub.library()) or "none downloaded yet"
        return oai_error(404, f"Model '{body.get('model')}' isn't downloaded. Available: {names}.")
    except (RuntimeError, ValueError) as e:
        return oai_error(503, str(e))
    body["model"] = model["id"]
    t0 = time.monotonic()
    try:
        resp = await upstream.send(upstream.build_request("POST", path, json=body), stream=True)
    except httpx.HTTPError as e:
        return oai_error(503, f"The engine isn't answering: {e}")
    if not body.get("stream") or resp.status_code != 200:
        data = await resp.aread()
        await resp.aclose()
        if resp.status_code == 200:
            usage = json.loads(data).get("usage") or {}
            engine.record(t0, None, None, usage.get("completion_tokens"), usage.get("prompt_tokens"))
        return Response(data, resp.status_code, media_type=resp.headers.get("content-type"))
    return StreamingResponse(relay(resp, t0), media_type="text/event-stream")


async def relay(resp: httpx.Response, t0: float):
    """Pass SSE bytes through untouched while timing first token and counting tokens for the dashboard."""
    first = last = None
    tokens, usage, buf = 0, {}, b""
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                line = line.rstrip(b"\r")
                if not line.startswith(b"data: ") or line == b"data: [DONE]":
                    continue
                try:
                    obj = json.loads(line[6:])
                except ValueError:
                    continue
                usage = obj.get("usage") or usage
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content") or d.get("reasoning_content") or d.get("reasoning") or d.get("tool_calls") or ch.get("text"):
                        tokens += 1  # stream_interval is 1, so one chunk is about one token
                        last = time.monotonic()
                        first = first or last
    finally:
        await resp.aclose()
        engine.record(t0, first, last, usage.get("completion_tokens") or tokens, usage.get("prompt_tokens"))


# ---------- management ----------

class RepoReq(BaseModel):
    repo: str


class LoadReq(BaseModel):
    model: str
    context: int | None = None


class TokenReq(BaseModel):
    token: str


def _gpu_budget() -> tuple[dict, int]:
    g = hub.gpu()
    return g, hub.budget(g, engine.used_mib(g))


@app.get("/manage/status")
def status():
    try:
        g = hub.gpu()
    except Exception as e:  # no GPU visible inside WSL: the UI should say so, not crash
        g = {"error": str(e)}
    return {"version": __version__, "gpu": g, "engine": engine.status(), "downloads": downloads.list()}


@app.get("/manage/catalog")
def catalog():
    g, b = _gpu_budget()
    return hub.catalog(g["compute_cap"], b)


@app.get("/manage/search")
def search(q: str, limit: int = 30):
    g, b = _gpu_budget()
    return hub.search(q, g["compute_cap"], b, min(limit, 100))


@app.get("/manage/variants")
def variants(repo: str):
    g, b = _gpu_budget()
    return hub.variants(repo, g["compute_cap"], b)


@app.get("/manage/inspect")
def inspect(repo: str):
    from huggingface_hub.errors import RepositoryNotFoundError

    g, b = _gpu_budget()
    try:
        return hub.inspect(repo, g["compute_cap"], b)
    except RepositoryNotFoundError:
        raise HTTPException(404, f"{repo} doesn't exist on Hugging Face (or is private).")


@app.get("/manage/about")
def about(repo: str):
    from huggingface_hub.errors import RepositoryNotFoundError

    try:
        return hub.about(repo)
    except RepositoryNotFoundError:
        raise HTTPException(404, f"{repo} doesn't exist on Hugging Face (or is private).")


@app.get("/manage/avatar")
def avatar(owner: str):
    return {"url": hub.avatar(owner)}


@app.get("/manage/library")
def library():
    g, b = _gpu_budget()
    return [{**m, "fit": hub.local_fit(m, b), "loaded": bool(engine.model and engine.model["id"] == m["id"])}
            for m in hub.library()]


@app.delete("/manage/library/{mid}", status_code=204)
async def delete_model(mid: str):
    if engine.model and engine.model["id"] == mid:
        raise HTTPException(409, "Unload the model before deleting it.")
    if mid in downloads.procs:
        downloads.cancel(mid)
        await asyncio.sleep(1)
    try:
        hub.delete_local(mid)
    except KeyError:
        raise HTTPException(404, f"No model '{mid}'.")
    downloads.jobs.pop(mid, None)


@app.get("/manage/downloads")
def list_downloads():
    return downloads.list()


@app.post("/manage/downloads", status_code=202)
async def start_download(req: RepoReq):
    g, b = await asyncio.to_thread(_gpu_budget)
    info = await asyncio.to_thread(hub.inspect, req.repo, g["compute_cap"], b)
    if not info["ok"]:
        raise HTTPException(400, info["reason"])
    return await downloads.start(info)


@app.delete("/manage/downloads/{mid}", status_code=204)
async def cancel_download(mid: str):
    try:
        downloads.cancel(mid)
    except KeyError:
        raise HTTPException(404, f"No running download '{mid}'.")


@app.post("/manage/load", status_code=202)
async def load(req: LoadReq):
    try:
        await engine.load(req.model, req.context)
    except KeyError:
        raise HTTPException(404, f"No downloaded model '{req.model}'.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return engine.status()


@app.post("/manage/unload")
async def unload():
    await engine.unload()
    return engine.status()


@app.get("/manage/logs")
def logs(n: int = 200):
    return {"lines": list(engine.log)[-n:]}


@app.get("/manage/stats")
def stats():
    return list(engine.stats)


@app.get("/manage/hermes")
def hermes_status():
    return hermes.installs()


@app.post("/manage/hermes/connect")
def hermes_connect():
    g, b = _gpu_budget()
    ready = []
    for m in hub.library():
        loaded = engine.model and engine.model["id"] == m["id"]
        ctx = engine.context if loaded else hub.local_fit(m, b)["default_context"]
        if ctx >= HERMES_MIN_CONTEXT and m["launch"].get("tools"):
            ready.append((m["id"], ctx, (m.get("role") == "coding", bool(loaded))))
    if not ready:
        raise HTTPException(400, "No downloaded model can run with Hermes Agent: it needs tool calling and "
                                 f"{HERMES_MIN_CONTEXT:,}+ tokens of context. Download one from the catalog first.")
    default = max(ready, key=lambda r: r[2])[0]  # prefer a coding model, then the loaded one
    return hermes.connect([(mid, ctx) for mid, ctx, _ in ready], default)


@app.post("/manage/hermes/disconnect")
def hermes_disconnect():
    return hermes.disconnect()


@app.get("/manage/hf/account")
def hf_account():
    return hub.account()


@app.put("/manage/hf/token")
def hf_login(req: TokenReq):
    try:
        return hub.login(req.token.strip())
    except Exception as e:
        raise HTTPException(400, f"Hugging Face rejected the token: {e}")


@app.delete("/manage/hf/token", status_code=204)
def hf_logout():
    hub.logout()

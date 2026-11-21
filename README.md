# Redline (on the Peregrine engine)

**Redline** is a Windows desktop app that runs LLMs on NVIDIA RTX GPUs with TensorRT-LLM. It installs everything itself, downloads models from Hugging Face the way LM Studio does, serves an OpenAI-compatible API, and registers itself with Hermes Agent.

- `app/`: the desktop app (Tauri 2 + Svelte). `npm run tauri dev` to work on it, `npm run tauri build` for the installer (`app/src-tauri/target/release/bundle/nsis/`). `npm run dev` alone runs the UI in a browser against a running backend; add `?setup=check|install|load|ready` to see each setup step.
- `backend/` and `bootstrap.ps1`: everything below. The installer ships both as resources.
- Data lives in `%LOCALAPPDATA%\PeregrineData` (the WSL distro and its download cache), apart from the app itself, so uninstalling the app keeps your models. `bootstrap.ps1 uninstall -Yes` removes the data.
- Chat threads are saved to `%APPDATA%\com.peregrine.app\threads.json`. Closing the app stops the engine and frees the GPU.

## How it fits together

```
Desktop shell (UI, later) ──runs──> bootstrap.ps1  (Windows side: GPU/WSL checks, distro install, start/stop)
        │
        └──HTTP──> 127.0.0.1:47800  gateway (FastAPI, inside the "Peregrine" WSL distro)
                     ├─ /v1/*      OpenAI-compatible proxy ──> trtllm-serve on :47801 (one model at a time)
                     ├─ /manage/*  catalog, HF search, downloads, library, load/unload, Hermes, HF token
                     └─ Hermes Agent (WSL and Windows installs) uses /v1 as provider "peregrine"
```

TensorRT-LLM dropped Windows after 0.17, so it runs in a dedicated Ubuntu 24.04 WSL distro that the app creates. That distro leaves your own distros alone and can be removed with `wsl --unregister Peregrine`. All WSL2 distros share one network namespace, and Windows reaches WSL's localhost through forwarding, so `http://127.0.0.1:47800/v1` works from Windows, from every distro, and for both Hermes installs.

Pinned stack: TensorRT-LLM 1.3.0, PyTorch 2.14.0+cu132, CUDA 13.4 (nvcc/NVRTC only, for TensorRT-LLM's JIT kernels), Python 3.12.

## Commands

```powershell
.\bootstrap.ps1 check       # GPU, driver, WSL, distro and backend state as JSON
.\bootstrap.ps1 install     # create the distro, copy backend/, run setup.sh (~7-10 GB download). Re-run to repair.
.\bootstrap.ps1 start       # run the gateway (blocks)
.\bootstrap.ps1 sync        # dev loop: copy backend/ into the distro again, then restart
.\bootstrap.ps1 stop
.\bootstrap.ps1 uninstall -Yes   # deletes the distro and every model in it
```

Every command prints JSON lines. Exit code 2 means "run as administrator" (WSL missing). Exit code 3 means "reboot, then run install again".

Tests (offline, rules and VRAM math): `python backend/tests/test_hub.py`.

## API

OpenAI-compatible, for chat UIs and agents:

| Route | Notes |
|---|---|
| `GET /v1/models` | Downloaded models, each with `context_length`. Hermes reads the window from here. |
| `POST /v1/chat/completions`, `/v1/completions` | Proxied to trtllm-serve. A request for a downloaded but unloaded model loads it first (JIT, takes minutes). Unknown request fields are dropped because trtllm-serve rejects them. |

Management, for the desktop UI:

| Route | Notes |
|---|---|
| `GET /manage/status` | GPU (VRAM, utilization), engine state (`stopped`/`starting`/`ready`/`error`), last request speed, downloads |
| `GET /manage/catalog` | Curated models with full compatibility verdict and VRAM fit for this GPU |
| `GET /manage/search?q=` | HF search. `status` is `ok`, `no` (with a reason) or `check` (needs inspect) |
| `GET /manage/variants?repo=` | A base model plus its quantized repos, compatible first. TensorRT-LLM's version of LM Studio's quant picker. |
| `GET /manage/inspect?repo=` | Full verdict: quantization, download size, max context that fits, Hermes-ready, tool and reasoning parsers |
| `GET/POST /manage/downloads`, `DELETE /manage/downloads/{id}` | Start (`{"repo": ...}`), list with byte progress, cancel |
| `GET /manage/library`, `DELETE /manage/library/{id}` | Downloaded models with live fit |
| `POST /manage/load` `{"model", "context"?}`, `POST /manage/unload` | One model at a time. First load of a model takes 3-6 minutes (JIT compile). Later loads take about a minute. |
| `GET /manage/logs?n=`, `GET /manage/stats` | Engine log tail; time to first token and tokens/s per request |
| `GET /manage/hermes`, `POST /manage/hermes/connect`, `POST /manage/hermes/disconnect` | Finds Hermes Agent installs and adds or removes `providers.peregrine` |
| `GET /manage/hf/account`, `PUT /manage/hf/token`, `DELETE /manage/hf/token` | Hugging Face login for gated models |

## Hermes Agent

`POST /manage/hermes/connect` adds a `providers.peregrine` entry to every Hermes install it finds (WSL and native Windows). It registers each downloaded model that has tool calling and fits at least 64K tokens of context, which is the minimum Hermes accepts. Your own `model:` block is not touched, and `config.yaml` is backed up the first time. Then:

```
hermes chat --provider peregrine -m <model id>
```

or `/model custom:peregrine:<model id>` inside a session. Hermes Agent warns that Nous Hermes 3/4 models are not built for agent work, so the catalog lists them as chat models. For coding with Hermes Agent, pick a coding model from the catalog.

## Limits worth knowing

- Model formats: safetensors only, in BF16/FP16, FP8 (per-tensor), NVFP4 or MXFP4 on RTX 50-series. GGUF, AWQ and GPTQ repos don't run. Most "4-bit" repos people know from LM Studio are GGUF.
- No CPU offload. The whole model and its KV cache must fit in free VRAM, so a 16 GB card tops out around 14B at NVFP4.
- Speed: for one person chatting, expect roughly 1.0-1.3x LM Studio's decode speed at the same bit width. The big multiples come from many requests at once, such as agents running sub-tasks in parallel, plus prefix caching on long coding contexts.
- GeForce cards aren't on TensorRT-LLM's official hardware list, though RTX 50-series kernels exist and are being worked on.
- Passing the metadata checks doesn't guarantee a load. Both Qwen3.5-9B NVFP4 uploads crash TensorRT-LLM 1.3.0 (`KNOWN_BAD` in hub.py). Repos saved by a newer transformers than TensorRT-LLM ships get a warning.

## Tested on an RTX 5070 Ti (16 GB, driver 621)

| Model | Result |
|---|---|
| nvidia/NVIDIA-Nemotron-3-Nano-4B-FP8, 64K context | Loads in 111 s cold, ~60 s warm. 147-154 tokens/s, 30-40 ms to first token. Tool calls and reasoning come back parsed. Hermes Agent ran a file-read task through it with 13.5K-token prompts. |
| RedHatAI/Qwen3.5-9B-NVFP4 | Fails: vision rope type `axial` (config from transformers 5.21) |
| AxionML/Qwen3.5-9B-NVFP4 | Fails: TensorRT-LLM's Qwen3.5 weight loader raises IndexError, same code in 1.4.0rc4 |

The other catalog entries pass the metadata checks but haven't been loaded yet.

## Answer style

Every model in Redline's chat answers under two built-in rules, sent as the system prompt (`app/src/lib/skills.js`): Unslop always (plain, specific writing with no chatbot tells) and Ponytail when the question is about code (the least code that correctly solves it). Hermes Agent keeps its own prompt.

## Design

Black canvas, ultralight lowercase Inter for display, text links instead of buttons, and one accent that the loaded model's family picks, adjusted to read at 4.5:1 on both themes. The visual language follows the Verza site.

## Releases and updates

Redline updates itself from this repo's GitHub Releases. Each release carries the installer, its signature and a `latest.json`. The app checks a few seconds after it starts and shows an update button in the header. Updates must be signed with the Redline key (`%USERPROFILE%\.tauri\redline.key`, kept out of the repo), so nothing else can pose as one. When an update starts, the engine stops to free the GPU, and the new version copies its own backend into WSL on the next start.

To ship one: bump `version` in `app/src-tauri/tauri.conf.json`, then run `.\release.ps1 -Notes "what changed"`.

## License

All rights reserved: see [LICENSE](LICENSE). The code is public to read. Using, copying or modifying it needs written permission; the official release builds may be run for personal use.

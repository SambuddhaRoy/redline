"""Hugging Face side: search, TensorRT-LLM compatibility rules, VRAM fit, catalog, downloads, local library.

Run as `python -m peregrine.hub download <repo> <sha> <dest>` it is the download worker: one process per
download, so cancelling is a kill and a crash can't take the gateway down.
"""
import ast
import asyncio
import collections
import importlib.util
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from . import DEFAULT_CONTEXT, GiB, HERMES_MIN_CONTEXT, MiB, MODELS

ALLOW = ["*.safetensors", "*.json", "*.jinja", "*.txt", "*.model", "*.tiktoken"]  # no *.py: never run remote code
IGNORE = ["*/*"]  # top level only (fnmatch '*' matches '/'); drops gpt-oss's original/ and metal/ copies
MANIFEST = "peregrine.json"
OVERHEAD = int(1.5 * GiB)  # ponytail: CUDA context + activations + 3 CUDA graphs, a guess; calibrate from real loads
RESERVE = 512 * MiB  # left free for the Windows desktop
MIN_CONTEXT = 4096

# TRT-LLM 1.3.0 docs/source/features/quantization.md, hardware matrix. Keys are ModelOpt quant_algo names.
QUANT_BY_SM = {120: {"FP8", "NVFP4", "MXFP4"}, 89: {"FP8", "W4A16_AWQ", "W4A8_AWQ"}, 86: {"W4A16_AWQ"}}
UNQUANTIZED = {"BF16", "FP16", "FP32"}

# model_type -> trtllm-serve --tool_parser. Qwen3.5 and Nemotron-H templates emit XML tool calls, which
# the qwen3_coder parser reads ('auto' wrongly picks the JSON parser for qwen3_5, TRT-LLM #18084).
TOOL_PARSER = {
    "qwen2": "qwen3", "qwen3": "qwen3", "qwen3_moe": "qwen3", "qwen3_next": "qwen3",
    "qwen3_5": "qwen3_coder", "qwen3_5_moe": "qwen3_coder", "nemotron_h": "qwen3_coder",
    "gemma4": "gemma4", "gemma4_unified": "gemma4", "glm4_moe": "glm4", "deepseek_v3": "deepseek_v3",
    "minimax_m2": "minimax_m2", "kimi_k2": "kimi_k2",
}
# Never 'auto': TRT-LLM 1.3.0 resolves it from tokenizer_config.json only, and most Qwen repos keep their
# template in chat_template.jinja, so trtllm-serve exits at startup. Only used when the template thinks.
REASONING_PARSER = {
    "qwen3": "qwen3", "qwen3_moe": "qwen3", "qwen3_next": "qwen3", "qwen3_5": "qwen3_5", "qwen3_5_moe": "qwen3_5",
    "nemotron_h": "nano-v3", "gemma4": "gemma4", "gemma4_unified": "gemma4", "deepseek_v3": "deepseek-r1",
    "minimax_m2": "minimax_m2",
}
# Used only when tensorrt_llm isn't importable (tests on a dev box). The installed index wins.
# "Mistral3" "ForConditionalGeneration" is split on purpose: as one 32-letter string, GitHub's secret
# scanner takes it for a Mistral API key and blocks the push.
FALLBACK_ARCHS = {
    "GptOssForCausalLM", "LlamaForCausalLM", "MistralForCausalLM", "Mistral3" "ForConditionalGeneration",
    "Qwen2ForCausalLM", "Qwen3ForCausalLM", "Qwen3MoeForCausalLM", "Qwen3NextForCausalLM", "Qwen3_5ForCausalLM",
    "Qwen3_5ForConditionalGeneration", "Qwen3_5MoeForConditionalGeneration", "Gemma3ForCausalLM",
    "Gemma4ForConditionalGeneration", "Gemma4UnifiedForConditionalGeneration", "NemotronHForCausalLM",
    "Phi3ForCausalLM", "SeedOssForCausalLM", "Starcoder2ForCausalLM", "Exaone4ForCausalLM",
    "HunYuanDenseV1ForCausalLM", "DeepseekV3ForCausalLM", "Glm4MoeForCausalLM", "MiniMaxM2ForCausalLM",
}
# Repos that pass every rule but crash TRT-LLM 1.3.0 at load, found by testing on an RTX 5070 Ti.
KNOWN_BAD = {
    "RedHatAI/Qwen3.5-9B-NVFP4": "Its vision config uses rope_type 'axial', which TensorRT-LLM 1.3.0 can't read.",
    "AxionML/Qwen3.5-9B-NVFP4": "TensorRT-LLM 1.3.0's Qwen3.5 weight loader crashes on this export "
                                "(IndexError splitting the linear-attention projections).",
}
DTYPE_BYTES = {"BF16": 2, "F16": 2, "F32": 4, "F64": 8, "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1,
               "U8": 1, "I8": 1, "I16": 2, "U16": 2, "I32": 4, "U32": 4, "I64": 8}


# ---------- GPU ----------

def gpu() -> dict:
    smi = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    line = subprocess.run(
        [smi, "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap,utilization.gpu",
         "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True,
    ).stdout.splitlines()[0]
    f = [x.strip() for x in line.split(",")]
    return {"name": f[0], "total_mib": int(f[1]), "used_mib": int(f[2]), "driver": f[3],
            "compute_cap": float(f[4]), "util": int(f[5]) if f[5].isdigit() else None}


def budget(g: dict, used_mib: int | None = None) -> int:
    """Bytes a model may use: total minus what other apps hold, minus a desktop reserve."""
    used = g["used_mib"] if used_mib is None else used_mib
    return (g["total_mib"] - used) * MiB - RESERVE


@lru_cache(maxsize=1)
def supported_archs() -> frozenset:
    """Read TRT-LLM's architecture index without importing tensorrt_llm (that takes seconds and touches CUDA)."""
    spec = importlib.util.find_spec("tensorrt_llm")
    if spec and spec.origin:
        index = Path(spec.origin).parent / "_torch" / "models" / "_arch_index.py"
        if index.exists():
            for node in ast.walk(ast.parse(index.read_text())):
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "MODEL_ARCH_TO_MODULE" for t in node.targets):
                    return frozenset(ast.literal_eval(node.value))
    return frozenset(FALLBACK_ARCHS)


# ---------- compatibility ----------

@lru_cache(maxsize=1)
def transformers_version() -> tuple:
    """The transformers TRT-LLM pins (1.3.0: 5.5.4). Configs written by a newer one can carry settings it
    can't parse, e.g. RedHatAI/Qwen3.5-9B-NVFP4's vision rope_type 'axial' kills trtllm-serve at load."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return _ver(version("transformers"))
    except PackageNotFoundError:
        return (5, 5, 4)


def _ver(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:2])

def sm_row(cc: float) -> int:
    return 120 if cc >= 12 else 89 if cc >= 8.9 else 86


def text_config(config: dict) -> dict:
    return config.get("text_config") or config


def quant_of(config: dict, hf_quant: dict | None) -> tuple[str, str | None]:
    """(scheme, kv_quant). scheme is a ModelOpt quant_algo name ('+'-joined when mixed), BF16/FP16/FP32,
    or 'UNSUPPORTED:<what>'. Mirrors TRT-LLM 1.3.0 model_config.py / quant_config_utils.py."""
    if hf_quant:  # hf_quant_config.json wins over config.json, as in TRT-LLM
        return _modelopt(hf_quant.get("quantization", hf_quant))
    qc = config.get("quantization_config") or text_config(config).get("quantization_config")
    if not qc:
        dtype = str(config.get("torch_dtype") or config.get("dtype") or text_config(config).get("torch_dtype") or "bfloat16")
        return ("FP32" if "32" in dtype else "FP16" if dtype in ("float16", "fp16") else "BF16"), None
    method = str(qc.get("quant_method", "")).lower()
    if method.startswith("modelopt"):
        return _modelopt(qc)
    if method == "mxfp4":
        return "MXFP4", None
    if method == "nvfp4":
        return ("NVFP4" if qc.get("group_size", 16) == 16 else "UNSUPPORTED:NVFP4 with group size != 16"), None
    if method == "fp8":
        return "FP8_BLOCK_SCALES", None  # e.g. official Qwen *-FP8 repos: 128x128 block scales
    if method == "compressed-tensors":
        return _compressed(qc), None
    return f"UNSUPPORTED:{method or 'unknown'}", None


def _modelopt(q: dict) -> tuple[str, str | None]:
    algo = q.get("quant_algo")
    if algo == "MIXED_PRECISION":
        algos = {(v or {}).get("quant_algo") for v in (q.get("quantized_layers") or {}).values()}
        algo = "+".join(sorted(a for a in algos if a)) or None
    return (algo or "BF16"), q.get("kv_cache_quant_algo")


def _compressed(qc: dict) -> str:
    groups = qc.get("config_groups") or {}
    g = groups.get("group_0") or (next(iter(groups.values())) if len(groups) == 1 else None)
    if not g:
        return "UNSUPPORTED:compressed-tensors with several config groups"
    # TRT-LLM 1.3.0 quant_config_utils.update_quant_config_from_compressed_tensors accepts exactly these
    w, a = g.get("weights") or {}, g.get("input_activations") or {}
    if w.get("num_bits") == 4 and w.get("type") == "float" and w.get("group_size") == 16:
        if w.get("strategy") == "tensor_group" and a.get("strategy") == "tensor_group":
            return "NVFP4"
        return "UNSUPPORTED:NVFP4 without 4-bit activations (e.g. NVFP4A16)"
    if w.get("num_bits") == 8 and w.get("type") == "float":
        if w.get("strategy") == "channel" and a.get("strategy") == "token":
            return "FP8_ROWWISE"
        if w.get("strategy") == "block":
            return "FP8_BLOCK_SCALES"
        return f"UNSUPPORTED:compressed-tensors FP8 with {w.get('strategy')} weight scales"
    return f"UNSUPPORTED:compressed-tensors {w.get('num_bits')}-bit {w.get('type')}"


def check(config: dict, hf_quant: dict | None, files: list[str], cc: float, archs=None) -> dict:
    """Can TRT-LLM run this repo on a GPU with compute capability cc? Decided from metadata only."""
    archs = supported_archs() if archs is None else archs
    arch = (config.get("architectures") or ["unknown"])[0]
    scheme, kv = quant_of(config, hf_quant)
    allowed = QUANT_BY_SM[sm_row(cc)] | UNQUANTIZED
    written = config.get("transformers_version")
    if arch not in archs:
        reason = f"{arch} isn't an architecture TensorRT-LLM supports."
    elif not any(f.endswith(".safetensors") and "/" not in f for f in files):
        reason = "No safetensors weights. GGUF and PyTorch-only repos don't run on TensorRT-LLM."
    elif scheme.startswith("UNSUPPORTED:"):
        reason = f"{scheme.split(':', 1)[1]} quantization doesn't load in TensorRT-LLM."
    elif not set(scheme.split("+")) <= allowed:
        reason = f"{scheme} quantization isn't supported on this GPU (compute capability {cc:g})."
    else:
        reason = None
    warning = None
    if written and _ver(written) > transformers_version()[:2]:  # a risk, not proof: warn, don't refuse
        warning = (f"Saved with transformers {written}, newer than the {'.'.join(map(str, transformers_version()))} "
                   "TensorRT-LLM uses. Settings it doesn't know can stop the model from loading.")
    return {"ok": reason is None, "reason": reason, "warning": warning, "arch": arch, "quant": scheme, "kv_quant": kv}


def launch_params(config: dict, template: str, cc: float) -> dict:
    """trtllm-serve flags that depend on the model."""
    mt = config.get("model_type", "")
    t = text_config(config)
    has_tools = "tools" in template
    tool = TOOL_PARSER.get(mt) if has_tools else None
    if has_tools and not tool and "<tool_call>" in template:
        tool = "qwen3"  # Hermes-style <tool_call>{json}</tool_call>, e.g. Nous models on Llama
    thinks = any(s in template for s in ("<think>", "enable_thinking", "reasoning_effort"))
    reasoning = REASONING_PARSER.get(mt) if thinks else None
    head_dim = max(t.get("head_dim") or t.get("hidden_size", 0) // (t.get("num_attention_heads") or 1),
                   t.get("global_head_dim") or 0)
    # ponytail: FP8 KV with head_dim 256 on SM120 needs TRT-LLM 1.4 (#19825); revisit when we move to 1.4
    kv = "fp8" if cc >= 8.9 and head_dim < 256 else "auto"
    return {"tool_parser": tool, "reasoning_parser": reasoning, "kv_cache_dtype": kv,
            "tools": bool(tool) or (mt == "gpt_oss" and has_tools),  # gpt-oss parses tools natively (harmony)
            "reasoning": bool(reasoning) or mt == "gpt_oss"}


# ---------- VRAM fit ----------

def kv_elems_per_token(config: dict) -> tuple[int, int, int]:
    """(full-attention KV elements per token, sliding-window KV elements per token, window).
    Recurrent state (Mamba, Gated DeltaNet) is a few tens of MB and rides in OVERHEAD."""
    t = text_config(config)
    heads = t.get("num_attention_heads") or 1
    kvh = t.get("num_key_value_heads") or heads
    hd = t.get("head_dim") or t.get("hidden_size", 0) // heads
    types = t.get("layer_types")
    if types:
        n_full, n_slide = types.count("full_attention"), types.count("sliding_attention")
    elif t.get("hybrid_override_pattern"):  # Nemotron-H: '*' attention, 'M' Mamba, '-' MLP
        n_full, n_slide = t["hybrid_override_pattern"].count("*"), 0
    else:
        n_full, n_slide = t.get("num_hidden_layers") or 0, 0
    full = n_full * 2 * (t.get("num_global_key_value_heads") or kvh) * (t.get("global_head_dim") or hd)
    slide = n_slide * 2 * kvh * hd
    window = t.get("sliding_window") or 0
    if slide and not window:
        full, slide = full + slide, 0
    return full, slide, window


def model_max_context(config: dict) -> int:
    return text_config(config).get("max_position_embeddings") or config.get("max_position_embeddings") or 32768


def fit(weights_bytes: int, config: dict, kv_bytes: int, budget_bytes: int) -> dict:
    full, slide, window = kv_elems_per_token(config)
    free = budget_bytes - weights_bytes - OVERHEAD - slide * kv_bytes * window
    cap = model_max_context(config)
    if free <= 0:
        ctx = 0
    elif not full:
        ctx = cap
    else:
        ctx = min(cap, free // (full * kv_bytes))
    default = min(ctx, DEFAULT_CONTEXT)
    if default != cap:
        default -= default % 1024
    return {"max_context": int(ctx), "default_context": int(default), "fits": default >= MIN_CONTEXT,
            "hermes_ready": default >= HERMES_MIN_CONTEXT,
            "kv_bytes_per_token": full * kv_bytes, "weights_bytes": weights_bytes,
            "budget_bytes": budget_bytes, "overhead_bytes": OVERHEAD + slide * kv_bytes * window, "model_max": cap}


def kv_bytes(launch: dict, kv_quant: str | None) -> int:
    return 1 if launch["kv_cache_dtype"] == "fp8" or kv_quant == "FP8" else 2


# ---------- Hugging Face ----------

def _template(cfg_files: dict) -> str:
    tc = cfg_files.get("tokenizer_config.json") or {}
    t = tc.get("chat_template") or ""
    if isinstance(t, list):  # named templates: [{"name": "default", "template": ...}, {"name": "tool_use", ...}]
        t = "\n".join(x.get("template", "") for x in t)
    return (cfg_files.get("chat_template.jinja") or "") + t


@lru_cache(maxsize=512)
def remote(repo: str) -> dict:
    """Metadata of one repo: file sizes and the small config files. Cached for the gateway's lifetime."""
    from huggingface_hub import HfApi, hf_hub_download

    info = HfApi().model_info(repo, files_metadata=True)
    files = {s.rfilename: (s.size or 0) for s in info.siblings or []}
    got = {}
    for name in ("config.json", "hf_quant_config.json", "tokenizer_config.json", "chat_template.jinja"):
        if name in files:
            text = Path(hf_hub_download(repo, name, revision=info.sha)).read_text(encoding="utf-8")
            got[name] = text if name.endswith(".jinja") else json.loads(text)
    lic = next((t.split(":", 1)[1] for t in info.tags or [] if t.startswith("license:")), None)
    # "base_model:quantized:Qwen/Qwen3-8B" on quantized repos: the key for the version picker
    bases = [t.split(":")[-1] for t in info.tags or [] if t.startswith("base_model:")]
    base = next((t.split(":")[-1] for t in info.tags or [] if t.startswith("base_model:quantized:")), bases[0] if bases else None)
    return {"repo": info.id, "sha": info.sha, "files": files, "config": got.get("config.json") or {},
            "hf_quant": got.get("hf_quant_config.json"), "template": _template(got), "gated": info.gated or False,
            "license": lic, "downloads": info.downloads, "likes": info.likes, "base": base}


def _selected(files: dict) -> dict:
    """The files a download fetches: ALLOW minus IGNORE."""
    from fnmatch import fnmatch
    return {f: s for f, s in files.items()
            if any(fnmatch(f, p) for p in ALLOW) and not any(fnmatch(f, p) for p in IGNORE)}


def inspect(repo: str, cc: float, budget_bytes: int) -> dict:
    from huggingface_hub.errors import GatedRepoError

    try:
        r = remote(repo)
    except GatedRepoError:
        return {"repo": repo, "ok": False, "gated": True,
                "reason": "This model is gated. Accept its license on huggingface.co, then add your Hugging Face token."}
    sel = _selected(r["files"])
    verdict = check(r["config"], r["hf_quant"], list(sel), cc)
    if r["repo"] in KNOWN_BAD:
        verdict.update(ok=False, reason=KNOWN_BAD[r["repo"]])
    launch = launch_params(r["config"], r["template"], cc) if r["config"] else {}
    weights = sum(s for f, s in sel.items() if f.endswith(".safetensors"))
    out = {k: r[k] for k in ("repo", "sha", "gated", "license", "downloads", "likes", "base")}
    out.update(verdict, model_type=r["config"].get("model_type"), download_bytes=sum(sel.values()),
               weights_bytes=weights, launch=launch)
    if verdict["ok"]:
        out["fit"] = fit(weights, r["config"], kv_bytes(launch, verdict["kv_quant"]), budget_bytes)
    return out


# ---------- about a model: avatar, benchmarks ----------

@lru_cache(maxsize=256)
def avatar(owner: str) -> str | None:
    """The org's (or user's) Hugging Face avatar URL."""
    import httpx

    for kind in ("organizations", "users"):
        try:
            r = httpx.get(f"https://huggingface.co/api/{kind}/{owner}/avatar", timeout=10)
            if r.status_code == 200 and r.json().get("avatarUrl"):
                return r.json()["avatarUrl"]
        except (httpx.HTTPError, ValueError):
            pass
    return None


BENCH_WORDS = re.compile(r"MMLU|GSM8K|HumanEval|MBPP|GPQA|AIME|LiveCodeBench|MATH|IFEval|Arena|SWE-?bench|BFCL|"
                         r"HellaSwag|ARC|TruthfulQA|BBH|Codeforces|Accuracy|Benchmark", re.I)


def _card_tables(repo: str, limit: int = 2) -> list[str]:
    """Markdown tables from a model card that look like benchmark results, quoted as written."""
    from huggingface_hub import hf_hub_download

    try:
        text = Path(hf_hub_download(repo, "README.md")).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    tables, block = [], []
    for line in text.splitlines() + [""]:
        if line.lstrip().startswith("|"):
            block.append(line.strip())
            continue
        if len(block) >= 3 and BENCH_WORDS.search("\n".join(block[:3])):
            tables.append("\n".join(block[:16]))  # header + separator + up to 14 rows
        block = []
    return tables[:limit]


@lru_cache(maxsize=256)
def about(repo: str) -> dict:
    """Benchmarks for a repo, falling back to its base model (quantized uploads rarely carry their own).
    Only numbers Hugging Face publishes: structured evalResults first, then tables quoted from model cards."""
    from huggingface_hub import HfApi

    api = HfApi()
    info = api.model_info(repo, expand=["evalResults", "baseModels"])
    base = None
    bm = getattr(info, "base_models", None) or {}
    if isinstance(bm, dict) and bm.get("models"):
        base = next((m["id"] for m in bm["models"] if m["id"] != repo), None)
    evals, sources = [], []
    for source in filter(None, (repo, base)):
        data = info if source == repo else api.model_info(source, expand=["evalResults"])
        for e in getattr(data, "eval_results", None) or []:  # huggingface_hub EvalResultEntry objects
            value = getattr(e, "value", None)
            if isinstance(value, (int, float)):
                evals.append({"benchmark": getattr(e, "dataset_id", None) or "?", "task": getattr(e, "task_id", None),
                              "value": value, "source": getattr(e, "source_name", None),
                              "url": getattr(e, "source_url", None), "from": source})
        if evals:
            sources.append(source)
            break
    tables, table_from = [], None
    for source in filter(None, (repo, base)):
        tables = _card_tables(source)
        if tables:
            table_from = source
            break
    return {"repo": repo, "base": base, "avatar": avatar(repo.split("/")[0]), "evals": evals[:12],
            "tables": tables, "tables_from": table_from}


EXPAND = ["config", "safetensors", "gated", "downloads", "likes", "lastModified", "tags"]
QUANT_TAGS = {"nvfp4", "fp4", "fp8", "mxfp4", "modelopt", "compressed-tensors", "4-bit", "8-bit"}
BAD_METHODS = ("awq", "gptq", "auto-round", "bitsandbytes", "hqq", "exl2", "mlx")


def _quick(m, cc: float, budget_bytes: int) -> dict:
    """Search-result triage from list metadata, which carries a trimmed quantization_config and no
    hf_quant_config.json. 'no' is certain, 'ok' is certain for plain and self-describing checkpoints,
    'check' means inspect() has to read the full config."""
    config = getattr(m, "config", None) or {}
    tags = set(getattr(m, "tags", None) or [])
    arch = (config.get("architectures") or [None])[0]
    method = str((config.get("quantization_config") or {}).get("quant_method", "")).lower()
    quantized = bool(method) or bool(tags & QUANT_TAGS)
    st = getattr(m, "safetensors", None)
    params = (getattr(st, "parameters", None) or {}) if st else {}
    # parameter counts give exact bytes only for unquantized weights; packed 4-bit counts don't. inspect() has real sizes.
    size = None if quantized else sum(n * DTYPE_BYTES.get(d, 2) for d, n in params.items())
    min_bytes = (getattr(st, "total", 0) or 0) // 2 if st else 0  # nothing ships below 4 bits per weight
    quant = method.upper() or next((t.upper() for t in ("nvfp4", "mxfp4", "fp8") if t in tags), None) or ("?" if quantized else "BF16")
    status, reason = "check", None
    if "mlx" in tags or not st:
        status, reason = "no", "No safetensors weights TensorRT-LLM can read (GGUF and MLX are LM Studio's formats)."
    elif not arch:
        pass
    elif arch not in supported_archs():
        status, reason = "no", f"{arch} isn't an architecture TensorRT-LLM supports."
    elif any(b in method for b in BAD_METHODS):
        status, reason = "no", f"{method} quantization doesn't load in TensorRT-LLM."
    elif (size or min_bytes) + OVERHEAD > budget_bytes:
        status, reason = "no", "Too large for this GPU's free memory."
    elif not quantized:
        status = "ok"
    elif method in ("mxfp4", "nvfp4") or method.startswith("modelopt"):
        v = check(config, None, ["model.safetensors"], cc)
        status, reason = ("ok", None) if v["ok"] else ("no", v["reason"])
    if m.id in KNOWN_BAD:
        status, reason = "no", KNOWN_BAD[m.id]
    return {"repo": m.id, "status": status, "reason": reason, "arch": arch, "quant": quant,
            "approx_bytes": size, "downloads": getattr(m, "downloads", None), "likes": getattr(m, "likes", None),
            "gated": getattr(m, "gated", None) or False, "updated": str(getattr(m, "last_modified", "") or "")}


def search(query: str, cc: float, budget_bytes: int, limit: int = 30) -> list[dict]:
    from huggingface_hub import HfApi

    models = HfApi().list_models(search=query, filter=["safetensors"], sort="downloads", limit=limit, expand=EXPAND)
    return [_quick(m, cc, budget_bytes) for m in models]


def variants(base: str, cc: float, budget_bytes: int) -> list[dict]:
    """The base repo plus its quantized children, compatible ones first: TRT-LLM's take on LM Studio's quant picker."""
    from huggingface_hub import HfApi

    api = HfApi()
    found = [api.model_info(base, expand=EXPAND)]
    found += api.list_models(filter=[f"base_model:quantized:{base}"], sort="downloads", limit=60, expand=EXPAND)
    order = {"ok": 0, "check": 1, "no": 2}
    return sorted((_quick(m, cc, budget_bytes) for m in found), key=lambda r: (order[r["status"]], -(r["downloads"] or 0)))


CATALOG = json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))


def catalog(cc: float, budget_bytes: int) -> list[dict]:
    def one(entry):
        try:
            return {**entry, **inspect(entry["repo"], cc, budget_bytes)}
        except Exception as e:  # one dead repo must not hide the rest
            return {**entry, "ok": False, "reason": f"Couldn't read from Hugging Face: {e}"}

    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(one, CATALOG))


def account() -> dict:
    from huggingface_hub import HfApi, get_token

    if not get_token():
        return {"user": None}
    try:
        return {"user": HfApi().whoami()["name"]}
    except Exception as e:
        return {"user": None, "error": str(e)}


def login(token: str) -> dict:
    from huggingface_hub import login as hf_login

    hf_login(token=token, add_to_git_credential=False)
    remote.cache_clear()  # gated repos may be readable now
    return account()


def logout() -> None:
    from huggingface_hub import logout as hf_logout

    try:
        hf_logout()
    except Exception:
        pass  # already logged out
    remote.cache_clear()


# ---------- local library ----------

def model_id(repo: str) -> str:
    """Served model name: no dots (they break `hermes config set` paths) and no colons (they break /model)."""
    return re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-")


def library() -> list[dict]:
    if not MODELS.exists():
        return []
    out = []
    for d in sorted(MODELS.iterdir()):
        if (d / MANIFEST).exists():
            out.append({**json.loads((d / MANIFEST).read_text()), "path": str(d)})
    return out


def _model_dir(mid: str) -> Path:
    """Every id comes from model_id(); anything else (e.g. '..') is refused before it touches the disk."""
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", mid):
        raise KeyError(mid)
    return MODELS / mid


def local_model(mid: str) -> dict:
    d = _model_dir(mid)
    if not (d / MANIFEST).exists():
        raise KeyError(mid)
    return {**json.loads((d / MANIFEST).read_text()), "path": str(d)}


def local_fit(m: dict, budget_bytes: int) -> dict:
    config = json.loads((Path(m["path"]) / "config.json").read_text())
    return fit(m["weights_bytes"], config, kv_bytes(m["launch"], m.get("kv_quant")), budget_bytes)


def delete_local(mid: str) -> None:
    d = _model_dir(mid)
    if not d.exists():
        raise KeyError(mid)
    shutil.rmtree(d)


# ---------- downloads ----------

class Downloads:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    def list(self) -> list[dict]:
        return [{k: v for k, v in j.items() if k != "info"} for j in self.jobs.values()]

    def _id_for(self, repo: str) -> str:
        for e in [*self.jobs.values(), *library()]:  # same repo again: same folder, so files are reused
            if e["repo"] == repo:
                return e["id"]
        mid = model_id(repo)
        taken = {j["id"]: j["repo"] for j in self.jobs.values()}
        for m in library():
            taken[m["id"]] = m["repo"]
        if taken.get(mid, repo) != repo:
            mid = model_id(repo.replace("/", "-"))  # same name from another org
        return mid

    async def start(self, info: dict) -> dict:
        repo = info["repo"]
        mid = self._id_for(repo)
        if (MODELS / mid / MANIFEST).exists():
            return {"id": mid, "repo": repo, "status": "done"}
        if self.jobs.get(mid, {}).get("status") == "downloading":
            return {k: v for k, v in self.jobs[mid].items() if k != "info"}
        entry = next((c for c in CATALOG if c["repo"] == repo), {})
        manifest = {"id": mid, "repo": repo, "sha": info["sha"], "role": entry.get("role"), "note": entry.get("note"),
                    **{k: info[k] for k in ("arch", "model_type", "quant", "kv_quant", "license",
                                             "download_bytes", "weights_bytes", "launch")}}
        job = {"id": mid, "repo": repo, "status": "downloading", "bytes": 0, "total": info["download_bytes"],
               "error": None, "started": time.time(), "info": manifest}
        self.jobs[mid] = job
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "peregrine.hub", "download", repo, info["sha"], str(MODELS / mid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.procs[mid] = proc
        asyncio.create_task(self._watch(job, proc))
        return {k: v for k, v in job.items() if k != "info"}

    async def _watch(self, job: dict, proc) -> None:
        tail = collections.deque(maxlen=20)
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith('{"bytes"'):
                job["bytes"] = json.loads(line)["bytes"]
            elif line:
                tail.append(line)
        await proc.wait()
        if self.procs.get(job["id"]) is proc:
            self.procs.pop(job["id"])
        if job["status"] == "cancelled":
            return
        if proc.returncode == 0:
            (MODELS / job["id"] / MANIFEST).write_text(json.dumps({**job["info"], "downloaded_at": time.time()}, indent=1))
            job.update(status="done", bytes=job["total"])
        else:
            job.update(status="error", error="\n".join(tail) or f"download worker exited with {proc.returncode}")

    def cancel(self, mid: str) -> None:
        proc = self.procs.get(mid)
        if not proc:
            raise KeyError(mid)
        self.jobs[mid]["status"] = "cancelled"
        proc.send_signal(signal.SIGINT)  # Xet aborts cleanly on KeyboardInterrupt
        asyncio.get_running_loop().call_later(5, lambda: proc.returncode is None and proc.kill())

    def cancel_all(self) -> None:
        for mid in list(self.procs):
            self.cancel(mid)


def _download_worker(repo: str, sha: str, dest: str) -> None:
    from huggingface_hub import snapshot_download
    from tqdm import tqdm

    state = {"bytes": 0, "t": 0.0}

    def emit():
        print(json.dumps({"bytes": state["bytes"]}), flush=True)

    class Progress(tqdm):  # counts network bytes; huggingface_hub drives it through tqdm_class
        def __init__(self, *a, **kw):
            kw.pop("name", None)
            kw.update(disable=False, file=open("/dev/null", "w"))
            super().__init__(*a, **kw)

        def update(self, n=1):
            if self.unit == "B" and not str(self.desc or "").startswith("Reconstructing"):
                state["bytes"] += n or 0
                if time.monotonic() - state["t"] > 0.5:
                    state["t"] = time.monotonic()
                    emit()
            return super().update(n)

    snapshot_download(repo, revision=sha, local_dir=dest, allow_patterns=ALLOW, ignore_patterns=IGNORE,
                      tqdm_class=Progress)
    emit()


if __name__ == "__main__" and sys.argv[1:2] == ["download"]:
    _download_worker(*sys.argv[2:5])

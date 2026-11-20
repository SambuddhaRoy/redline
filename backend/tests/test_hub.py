"""Offline checks for the compatibility rules, parser choice and VRAM math. Run: python tests/test_hub.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peregrine import GiB, hub  # noqa: E402

QWEN3_8B = {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "num_hidden_layers": 36,
            "num_attention_heads": 32, "num_key_value_heads": 8, "head_dim": 128, "hidden_size": 4096,
            "max_position_embeddings": 40960, "torch_dtype": "bfloat16"}
QWEN35_9B = {"architectures": ["Qwen3_5ForConditionalGeneration"], "model_type": "qwen3_5",
             "text_config": {"num_hidden_layers": 32, "num_attention_heads": 16, "num_key_value_heads": 4,
                             "head_dim": 256, "hidden_size": 4096, "max_position_embeddings": 262144,
                             "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 8}}
GPT_OSS = {"architectures": ["GptOssForCausalLM"], "model_type": "gpt_oss", "num_hidden_layers": 24,
           "num_attention_heads": 64, "num_key_value_heads": 8, "head_dim": 64, "sliding_window": 128,
           "layer_types": ["sliding_attention", "full_attention"] * 12, "max_position_embeddings": 131072,
           "quantization_config": {"quant_method": "mxfp4"}}
ST = ["model.safetensors", "config.json"]
NVFP4_CT = {"quant_method": "compressed-tensors", "config_groups": {"group_0": {
    "weights": {"num_bits": 4, "type": "float", "strategy": "tensor_group", "group_size": 16},
    "input_activations": {"num_bits": 4, "type": "float", "strategy": "tensor_group", "group_size": 16}}}}


def ok(config, cc=12.0, hf_quant=None, files=ST):
    return hub.check(config, hf_quant, files, cc, archs=hub.FALLBACK_ARCHS)


def test_compat():
    modelopt_nvfp4 = {"quantization": {"quant_algo": "NVFP4", "kv_cache_quant_algo": "FP8"}}
    r = ok(QWEN3_8B, hf_quant=modelopt_nvfp4)
    assert r["ok"] and r["quant"] == "NVFP4" and r["kv_quant"] == "FP8", r
    assert not ok(QWEN3_8B, cc=8.9, hf_quant=modelopt_nvfp4)["ok"]  # no NVFP4 on Ada
    assert ok({**QWEN3_8B, "quantization_config": NVFP4_CT})["quant"] == "NVFP4"
    a16 = {"quant_method": "compressed-tensors", "config_groups": {"group_0": {
        **NVFP4_CT["config_groups"]["group_0"], "input_activations": None}}}
    assert not ok({**QWEN3_8B, "quantization_config": a16})["ok"]
    assert not ok({**QWEN3_8B, "quantization_config": {"quant_method": "awq"}})["ok"]
    assert not ok({**QWEN3_8B, "quantization_config": {"quant_method": "fp8", "weight_block_size": [128, 128]}})["ok"]
    rowwise = {"quant_method": "compressed-tensors", "config_groups": {"group_0": {
        "weights": {"num_bits": 8, "type": "float", "strategy": "channel"},
        "input_activations": {"num_bits": 8, "type": "float", "strategy": "token"}}}}
    assert ok({**QWEN3_8B, "quantization_config": rowwise})["quant"] == "FP8_ROWWISE"
    assert not ok({**QWEN3_8B, "quantization_config": rowwise})["ok"]
    per_tensor = {"quant_method": "compressed-tensors", "config_groups": {"group_0": {
        "weights": {"num_bits": 8, "type": "float", "strategy": "tensor"},
        "input_activations": {"num_bits": 8, "type": "float", "strategy": "tensor"}}}}
    assert not ok({**QWEN3_8B, "quantization_config": per_tensor})["ok"]  # TRT-LLM raises on 'tensor'
    assert ok(GPT_OSS)["ok"] and not ok(GPT_OSS, cc=8.9)["ok"]
    assert ok(QWEN3_8B, cc=8.6)["ok"]  # unquantized BF16 runs anywhere
    assert "safetensors" in ok(QWEN3_8B, files=["model.gguf"])["reason"]
    assert "safetensors" in ok(QWEN3_8B, files=["sub/model.safetensors"])["reason"]
    assert "isn't an architecture" in ok({**QWEN3_8B, "architectures": ["DoryForCausalLM"]})["reason"]
    mixed = {"quantization": {"quant_algo": "MIXED_PRECISION", "quantized_layers": {
        "a": {"quant_algo": "FP8"}, "b": {"quant_algo": "NVFP4"}}}}
    assert ok(QWEN3_8B, hf_quant=mixed)["ok"]
    newer = ok({**QWEN3_8B, "transformers_version": "5.21.0"})
    assert newer["ok"] and "newer than" in newer["warning"]
    assert ok({**QWEN3_8B, "transformers_version": "5.2.0"})["ok"]


def test_launch():
    p = hub.launch_params(QWEN35_9B, "{% if tools %}<tool_call><function=...>{% endif %}<think>", 12.0)
    assert p == {"tool_parser": "qwen3_coder", "reasoning_parser": "qwen3_5", "kv_cache_dtype": "auto",
                 "tools": True, "reasoning": True}, p
    p = hub.launch_params(QWEN3_8B, "{% if tools %}<tool_call>{% endif %}", 12.0)
    assert p["tool_parser"] == "qwen3" and p["reasoning_parser"] is None and p["kv_cache_dtype"] == "fp8"
    llama_hermes = {"architectures": ["LlamaForCausalLM"], "model_type": "llama", "num_attention_heads": 24,
                    "hidden_size": 3072}
    assert hub.launch_params(llama_hermes, "tools ... <tool_call>", 12.0)["tool_parser"] == "qwen3"
    assert hub.launch_params(llama_hermes, "plain chat", 12.0)["tools"] is False
    p = hub.launch_params(GPT_OSS, "tools reasoning_effort", 12.0)
    assert p["tools"] and p["reasoning"] and p["tool_parser"] is None
    assert hub.launch_params(QWEN3_8B, "", 8.6)["kv_cache_dtype"] == "auto"  # no FP8 KV before Ada


def test_vram():
    assert hub.kv_elems_per_token(QWEN3_8B) == (36 * 2 * 8 * 128, 0, 0)  # 144 KiB/token in BF16, as measured
    assert hub.kv_elems_per_token(QWEN35_9B)[0] == 8 * 2 * 4 * 256  # only the 8 full-attention layers hold KV
    assert hub.kv_elems_per_token(GPT_OSS) == (12 * 2 * 8 * 64, 12 * 2 * 8 * 64, 128)
    f = hub.fit(int(6.4e9), QWEN3_8B, 1, 12 * GiB)
    assert f["max_context"] == 40960 and f["default_context"] == 40960 and not f["hermes_ready"], f
    f = hub.fit(int(9.36e9), QWEN35_9B, 2, 13 * GiB)
    assert f["hermes_ready"] and f["default_context"] == 65536, f
    f = hub.fit(int(9.36e9), QWEN35_9B, 2, 12 * GiB)  # BF16 KV at 12 GiB lands just under Hermes's floor
    assert f["fits"] and not f["hermes_ready"], f
    f = hub.fit(int(13.76e9), GPT_OSS, 2, 12 * GiB)
    assert not f["fits"] and f["max_context"] == 0, f
    f = hub.fit(int(6.4e9), QWEN3_8B, 1, int(8.5 * GiB))  # tight budget: default rounds down to a 1024 multiple
    assert 0 < f["default_context"] < 40960 and f["default_context"] % 1024 == 0, f


def test_ids():
    assert hub.model_id("RedHatAI/Qwen3.5-9B-NVFP4") == "qwen3-5-9b-nvfp4"
    assert hub.model_id("openai/gpt-oss-20b") == "gpt-oss-20b"


def test_model_dir_guard():
    for bad in ("..", ".", "", "a/b", "../models", "Qwen3.5"):
        try:
            hub._model_dir(bad)
        except KeyError:
            continue
        raise AssertionError(bad)
    assert hub._model_dir("qwen3-5-9b-nvfp4").name == "qwen3-5-9b-nvfp4"


def test_sanitize():
    try:
        from peregrine.api import sanitize
    except ImportError:  # fastapi only exists inside the distro
        return
    body = {"model": "x", "messages": [], "options": {"num_ctx": 1}, "think": True, "reasoning_effort": "xhigh"}
    assert sanitize(body) == {"model": "x", "messages": [], "reasoning_effort": "high"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

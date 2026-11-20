"""Peregrine: TensorRT-LLM on RTX GPUs, with Hugging Face downloads and an OpenAI-compatible API."""
import os
from pathlib import Path

__version__ = "0.1.0"

DATA = Path(os.environ.get("PEREGRINE_DATA", "/var/lib/peregrine"))
MODELS = DATA / "models"
LOGS = DATA / "logs"
VENV_BIN = Path(os.environ.get("PEREGRINE_VENV_BIN", "/opt/peregrine/venv/bin"))
GATEWAY_PORT = int(os.environ.get("PEREGRINE_PORT", "47800"))
ENGINE_PORT = GATEWAY_PORT + 1  # trtllm-serve, never exposed directly

MiB, GiB = 1 << 20, 1 << 30
HERMES_MIN_CONTEXT = 64_000  # Hermes Agent refuses smaller windows (agent/model_metadata.py)
DEFAULT_CONTEXT = 65_536

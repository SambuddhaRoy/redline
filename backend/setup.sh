#!/usr/bin/env bash
# Runs as root inside the Peregrine WSL distro. Safe to re-run (it doubles as repair/update).
# stdout: one JSON line per step for the desktop shell. Everything else goes to $LOG.
set -euo pipefail

TRTLLM=1.3.0
TORCH=2.14.0
TORCH_INDEX=https://download.pytorch.org/whl/cu132   # cu132, not the default cu130: SM120 XQA JIT breaks on NVRTC 13.0 (TRT-LLM #20944)
CUDA_V=13-4                                          # TRT-LLM 1.3.0 is built on CUDA 13.4
ROOT=/opt/peregrine
VENV=$ROOT/venv
DATA=/var/lib/peregrine
LOG=$DATA/logs/setup.log

mkdir -p "$DATA"/{models,hf,logs,cache}
exec 3>&1 >>"$LOG" 2>&1
echo "=== setup $(date -Is) ==="
step() { printf '{"step":"%s","status":"%s"}\n' "$1" "$2" >&3; }
CUR=start
trap 'printf "{\"step\":\"%s\",\"status\":\"error\",\"log\":\"%s\"}\n" "$CUR" "$LOG" >&3' ERR

CUR=user; step $CUR start
# A uid-1000 user and a default in wsl.conf keep Ubuntu's first-run prompt away if someone opens the distro.
id peregrine >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash peregrine
grep -q '^\[user\]' /etc/wsl.conf 2>/dev/null || printf '\n[user]\ndefault=peregrine\n' >>/etc/wsl.conf
step $CUR done

CUR=apt; step $CUR start
export DEBIAN_FRONTEND=noninteractive
dpkg --configure -a     # finish anything a killed earlier run left half-installed
apt-get install -y -f
apt-get update
apt-get install -y python3.12-venv python3-dev build-essential libopenmpi-dev openmpi-bin git curl ca-certificates
step $CUR done

CUR=cuda; step $CUR start
# nvcc + NVRTC + headers for TRT-LLM's JIT kernels. Never install a Linux GPU driver in WSL.
# Use the ubuntu2404 repo: the wsl-ubuntu repo stops at CUDA 13.3.
if [ ! -x /usr/local/cuda/bin/nvcc ]; then
  curl -fsSLo /tmp/cuda-keyring.deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
  dpkg -i /tmp/cuda-keyring.deb
  apt-get update
  # ponytail: the NGC image's component list (~0.25 GB); falls back to the full toolkit (~3.5 GB) if a name is missing
  apt-get install -y cuda-nvcc-$CUDA_V cuda-crt-$CUDA_V cuda-cudart-dev-$CUDA_V cuda-nvrtc-dev-$CUDA_V \
    cuda-driver-dev-$CUDA_V cuda-culibos-dev-$CUDA_V cuda-cupti-$CUDA_V cuda-cccl-$CUDA_V \
    libcurand-dev-$CUDA_V \
    || apt-get install -y cuda-toolkit-$CUDA_V
  # TRT-LLM's FMHA JIT hard-codes /usr/local/cuda/bin/nvcc (#20515)
  [ -e /usr/local/cuda ] || ln -s "/usr/local/cuda-${CUDA_V/-/.}" /usr/local/cuda
fi
# FlashInfer's sampling JIT includes curand.h; checked on its own so older installs pick it up on re-run
[ -e /usr/local/cuda/include/curand.h ] || apt-get install -y libcurand-dev-$CUDA_V
/usr/local/cuda/bin/nvcc --version
step $CUR done

CUR=python; step $CUR start
[ -x "$VENV/bin/python" ] || python3.12 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip setuptools wheel
step $CUR done

CUR=torch; step $CUR start
"$VENV/bin/pip" install "torch==$TORCH" torchvision --index-url "$TORCH_INDEX"
# Pin the installed torch so the TRT-LLM install can't swap it for a cu130 build.
echo "torch==$("$VENV/bin/python" -c 'import torch; print(torch.__version__)')" >"$ROOT/constraints.txt"
step $CUR done

CUR=tensorrt_llm; step $CUR start
# PyPI only has an sdist; the 2.2 GB wheel lives on pypi.nvidia.com.
"$VENV/bin/pip" install "tensorrt_llm==$TRTLLM" --extra-index-url https://pypi.nvidia.com -c "$ROOT/constraints.txt"
step $CUR done

CUR=gateway; step $CUR start
"$VENV/bin/pip" install -c "$ROOT/constraints.txt" fastapi uvicorn httpx "huggingface_hub>=1.26" pyyaml
step $CUR done

CUR=verify; step $CUR start
"$VENV/bin/python" -c 'import tensorrt_llm, torch; print(tensorrt_llm.__version__, torch.__version__, torch.cuda.get_device_name(0))'
step $CUR done

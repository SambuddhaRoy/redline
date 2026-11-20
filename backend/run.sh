#!/usr/bin/env bash
# Starts the Peregrine gateway. trtllm-serve children inherit this environment.
export CUDA_HOME=/usr/local/cuda
export PATH=/usr/local/cuda/bin:/opt/peregrine/venv/bin:$PATH
export TRTLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export HF_HOME=/var/lib/peregrine/hf
export TORCHINDUCTOR_CACHE_DIR=/var/lib/peregrine/cache/inductor
export PYTHONPATH=/opt/peregrine/app
exec /opt/peregrine/venv/bin/python -m peregrine "$@"

#!/bin/bash
# Lance Cohere Transcribe (ASR) via vLLM — endpoint /v1/audio/transcriptions.
#
# Backend STT par défaut de TranscrIA. La LLM d'arbitrage NE passe PAS ici
# (elle reste sur llama.cpp, scripts/launch_arbitrage.sh).
#
# Variables d'environnement (avec défauts) :
#   VLLM_GPU       GPU dédié (CUDA_VISIBLE_DEVICES)        défaut: 3
#   VLLM_PORT      port HTTP                               défaut: 8001
#   VLLM_GPU_MEM   fraction VRAM utilisée                  défaut: 0.85
#   VLLM_MODEL     id/chemin du modèle                     défaut: CohereLabs/cohere-transcribe-03-2026
#
# Exemple : VLLM_GPU=5 VLLM_PORT=8001 ./scripts/launch_vllm_cohere.sh
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-12.8
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

VLLM_BIN="${VLLM_BIN:-/home/admin_ia/vllm_venv/bin/vllm}"
VLLM_GPU="${VLLM_GPU:-3}"
VLLM_PORT="${VLLM_PORT:-8001}"
VLLM_GPU_MEM="${VLLM_GPU_MEM:-0.85}"
VLLM_MODEL="${VLLM_MODEL:-CohereLabs/cohere-transcribe-03-2026}"
SERVED_NAME="${VLLM_SERVED_NAME:-cohere-transcribe}"

export CUDA_VISIBLE_DEVICES="$VLLM_GPU"

echo "[vllm-cohere] GPU=$VLLM_GPU port=$VLLM_PORT model=$VLLM_MODEL mem=$VLLM_GPU_MEM"
exec "$VLLM_BIN" serve "$VLLM_MODEL" \
  --trust-remote-code \
  --host 0.0.0.0 --port "$VLLM_PORT" \
  --gpu-memory-utilization "$VLLM_GPU_MEM" \
  --served-model-name "$SERVED_NAME"

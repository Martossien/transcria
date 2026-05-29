#!/bin/bash
# Lance Granite Speech 4.1 via vLLM — LLM audio-in (endpoint /v1/chat/completions).
#
# ⚠ Famille B (cf. docs/MIGRATION_API_SERVEUR_GPU.md §2.2) : modèle GÉNÉRATIF
# audio-in, pas un ASR dédié. Il ne renvoie PAS de timestamps ni de segments
# structurés — backend d'appoint, pas remplaçant direct de Cohere/Whisper.
#
# Variables d'environnement (avec défauts) :
#   VLLM_GPU       GPU dédié (CUDA_VISIBLE_DEVICES)        défaut: 6
#   VLLM_PORT      port HTTP                               défaut: 8006
#   VLLM_GPU_MEM   fraction VRAM utilisée                  défaut: 0.85
#   VLLM_MODEL     id/chemin du modèle                     défaut: ibm-granite/granite-speech-4.1-2b
#
# Exemple : VLLM_GPU=7 ./scripts/launch_vllm_granite.sh
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-12.8
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

VLLM_BIN="${VLLM_BIN:-/home/admin_ia/vllm_venv/bin/vllm}"
VLLM_GPU="${VLLM_GPU:-6}"
VLLM_PORT="${VLLM_PORT:-8006}"
VLLM_GPU_MEM="${VLLM_GPU_MEM:-0.85}"
VLLM_MODEL="${VLLM_MODEL:-ibm-granite/granite-speech-4.1-2b}"
SERVED_NAME="${VLLM_SERVED_NAME:-granite-speech}"

export CUDA_VISIBLE_DEVICES="$VLLM_GPU"

echo "[vllm-granite] GPU=$VLLM_GPU port=$VLLM_PORT model=$VLLM_MODEL mem=$VLLM_GPU_MEM"
exec "$VLLM_BIN" serve "$VLLM_MODEL" \
  --trust-remote-code \
  --host 0.0.0.0 --port "$VLLM_PORT" \
  --gpu-memory-utilization "$VLLM_GPU_MEM" \
  --served-model-name "$SERVED_NAME"

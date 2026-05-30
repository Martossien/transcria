#!/usr/bin/env bash
# Lance le modèle STT Cohere Transcribe (ASR), servi via une API compatible OpenAI
# (/v1/audio/transcriptions). Backend STT par défaut de TranscrIA. (La LLM
# d'arbitrage ne passe PAS par ici : elle reste sur llama.cpp, cf. launch_arbitrage.sh.)
#
# MOTEUR DE SERVING — non hardcodé, voir _stt_serve_lib.sh :
#   STT_ENGINE=vllm    (défaut)   `vllm serve …`
#   STT_ENGINE=sglang             `python -m sglang.launch_server …`
#   STT_ENGINE=custom             commande libre via STT_SERVE_CMD
#
# USAGE
#   source /home/admin_ia/vllm_venv/bin/activate          # (si engine=vllm)
#   ./scripts/launch_stt_cohere.sh
#   STT_GPU=3 STT_PORT=8003 ./scripts/launch_stt_cohere.sh
#   STT_ENGINE=sglang STT_BIN=python ./scripts/launch_stt_cohere.sh
#   scripts/stop_stt.sh --port 8003                        # pour arrêter
#   nohup setsid ./scripts/launch_stt_cohere.sh > /tmp/stt_cohere.log 2>&1 &   # persistant
#
# PARAMÈTRES (variables d'env, défauts ; anciens noms VLLM_* encore acceptés)
#   STT_GPU=3            GPU dédié (CUDA_VISIBLE_DEVICES)
#   STT_PORT=8003        port HTTP. 8003 (et non 8001) pour ne pas réserver 8002
#                        en EngineCore vLLM : 8002 = service inference_service.
#   STT_GPU_MEM=0.85     fraction de VRAM
#   STT_MODEL / STT_SERVED_NAME
#
# BON À SAVOIR
#   - L'endpoint rejette le MP3 en upload (bug connu) ; envoyez WAV/OGG.
#     TranscrIA convertit automatiquement (AudioConverter / RemoteTranscriber).
#   - Dépendances audio côté serveur vLLM : `pip install librosa soundfile`.
#   - Repères (RTX 3090) : démarrage à froid ~25 s, VRAM ~3.9 GiB, 74 s audio ~1.8 s.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

STT_LABEL="stt-cohere"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 3)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8003)"
STT_GPU_MEM="$(_stt_default STT_GPU_MEM VLLM_GPU_MEM 0.85)"
STT_MODEL="$(_stt_default STT_MODEL VLLM_MODEL CohereLabs/cohere-transcribe-03-2026)"
STT_SERVED_NAME="$(_stt_default STT_SERVED_NAME VLLM_SERVED_NAME cohere-transcribe)"
STT_TRUST_REMOTE="${STT_TRUST_REMOTE:-1}"

stt_serve

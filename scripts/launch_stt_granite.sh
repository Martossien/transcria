#!/usr/bin/env bash
# Lance le modèle Granite Speech 4.1, servi via une API compatible OpenAI.
#
# ⚠ Modèle GÉNÉRATIF audio-in (« famille B », cf. docs/MIGRATION_API_SERVEUR_GPU.md),
#   PAS un ASR dédié : il NE renvoie ni timestamps ni segments structurés, et se
#   teste via /v1/chat/completions (kind=omni). Backend d'appoint, pas un
#   remplaçant direct de Cohere/Whisper ; qualité en français moindre.
#
# MOTEUR DE SERVING — non hardcodé, voir _stt_serve_lib.sh :
#   STT_ENGINE=vllm (défaut) | sglang | custom
#
# USAGE
#   source vllm_venv/bin/activate          # (si engine=vllm)
#   ./scripts/launch_stt_granite.sh
#   STT_GPU=6 STT_PORT=8007 ./scripts/launch_stt_granite.sh
#   scripts/stop_stt.sh --port 8007                        # pour arrêter
#   nohup setsid ./scripts/launch_stt_granite.sh > /tmp/stt_granite.log 2>&1 &   # persistant
#
# PARAMÈTRES (variables d'env, défauts ; anciens noms VLLM_* encore acceptés)
#   STT_GPU=6            GPU dédié (CUDA_VISIBLE_DEVICES)
#   STT_PORT=8007        port HTTP. 8007 (et non 8006) : Whisper (8005) réserve
#                        8006 pour son EngineCore. 8008 voisin = EngineCore Granite.
#   STT_GPU_MEM=0.85     fraction de VRAM
#   STT_MODEL / STT_SERVED_NAME
#
# BON À SAVOIR
#   - Repères (RTX 3090) : démarrage à froid ~90 s, VRAM ~4.3 GiB, 74 s audio ~1.4 s.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

STT_LABEL="stt-granite"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 6)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8007)"
STT_GPU_MEM="$(_stt_default STT_GPU_MEM VLLM_GPU_MEM 0.85)"
STT_MODEL="$(_stt_default STT_MODEL VLLM_MODEL ibm-granite/granite-speech-4.1-2b)"
STT_SERVED_NAME="$(_stt_default STT_SERVED_NAME VLLM_SERVED_NAME granite-speech)"
STT_TRUST_REMOTE="${STT_TRUST_REMOTE:-1}"

stt_serve

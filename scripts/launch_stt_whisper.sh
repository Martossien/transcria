#!/usr/bin/env bash
# Lance le modèle STT Whisper large-v3 (ASR), servi via une API compatible OpenAI
# (/v1/audio/transcriptions).
#
# MOTEUR DE SERVING — non hardcodé, voir _stt_serve_lib.sh :
#   STT_ENGINE=vllm (défaut) | sglang | custom
#
# USAGE
#   source vllm_venv/bin/activate          # (si engine=vllm)
#   ./scripts/launch_stt_whisper.sh
#   STT_GPU=5 STT_PORT=8005 ./scripts/launch_stt_whisper.sh
#   scripts/stop_stt.sh --port 8005                        # pour arrêter
#   nohup setsid ./scripts/launch_stt_whisper.sh > /tmp/stt_whisper.log 2>&1 &   # persistant
#
# PARAMÈTRES (variables d'env, défauts ; anciens noms VLLM_* encore acceptés)
#   STT_GPU=5            GPU dédié (CUDA_VISIBLE_DEVICES)
#   STT_PORT=8005        port HTTP (8006 voisin réservé à l'EngineCore vLLM)
#   STT_GPU_MEM=0.85     fraction de VRAM
#   STT_MODEL / STT_SERVED_NAME
#
# BON À SAVOIR
#   - L'endpoint rejette le MP3 en upload (bug connu) ; envoyez WAV/OGG.
#     TranscrIA convertit automatiquement (AudioConverter / RemoteTranscriber).
#   - Dépendances audio côté serveur vLLM : `pip install librosa soundfile`.
#   - Repères (RTX 3090) : démarrage à froid ~105 s, VRAM ~2.9 GiB, 74 s audio ~1.8 s.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

STT_LABEL="stt-whisper"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 5)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8005)"
STT_GPU_MEM="$(_stt_default STT_GPU_MEM VLLM_GPU_MEM 0.85)"
STT_MODEL="$(_stt_default STT_MODEL VLLM_MODEL openai/whisper-large-v3)"
STT_SERVED_NAME="$(_stt_default STT_SERVED_NAME VLLM_SERVED_NAME whisper-large-v3)"
STT_TRUST_REMOTE="${STT_TRUST_REMOTE:-0}"

stt_serve

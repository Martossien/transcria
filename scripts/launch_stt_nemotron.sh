#!/usr/bin/env bash
# Lance le runtime STT servi parakeet.cpp avec Nemotron 3.5 ASR 0.6B (backend
# `nemotron`), API compatible OpenAI (/v1/audio/transcriptions).
#
# PRÉREQUIS (une fois) :
#   venv/bin/python -m transcria.installer.cli parakeetcpp
#   + le GGUF via la page « Modèles » (catalogue nemotron) ou :
#   hf download mudler/parakeet-cpp-gguf nemotron-3.5-asr-streaming-0.6b-f16.gguf \
#       --local-dir models/parakeet-cpp
#
# USAGE
#   STT_GPU=0 STT_PORT=8022 ./scripts/launch_stt_nemotron.sh
#   scripts/stop_stt.sh --port 8022
#
# SANTÉ (spike consigné 2026-07-12, commit épinglé) : PAS de /v1/models (404) —
# le manifeste resource_node.engines doit déclarer `health_path: /health`
# (répond 200 {"status":"ok"} une fois le GGUF chargé). Le warning
# AsrClient.health (« modèle absent de /models ») est ATTENDU et non bloquant.
# Le champ multipart `language` est toléré (pas de forçage serveur : Nemotron
# sort la langue source — surveiller EN % sur audio très dégradé).
#
# BON À SAVOIR
#   - STT_GPU_MEM est IGNORÉ par parakeet-server : la fraction du manifeste ne
#     sert qu'à l'admission VRAM (repère RTX 3090 : ~2 GiB, fenêtre 5 min ~2 s).
#   - Qualifié : WER 0,492 (8/8) sur notre benchmark de réunions réelles.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

RUNTIMES_DIR="${TRANSCRIA_RUNTIMES_DIR:-$REPO_ROOT/runtimes}"
PARAKEET_HOME="$RUNTIMES_DIR/parakeetcpp"
MODELS_DIR="${MODELS_DIR:-$REPO_ROOT/models}"

STT_LABEL="stt-nemotron"
STT_ENGINE="custom"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 0)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8022)"
STT_HOST="${STT_HOST:-0.0.0.0}"
STT_MODEL="${STT_MODEL:-$MODELS_DIR/parakeet-cpp/nemotron-3.5-asr-streaming-0.6b-f16.gguf}"

if [[ ! -x "$PARAKEET_HOME/bin/parakeet-server" ]]; then
    echo "[$STT_LABEL] ERREUR : $PARAKEET_HOME/bin/parakeet-server absent." >&2
    echo "[$STT_LABEL] Provisionner : venv/bin/python -m transcria.installer.cli parakeetcpp" >&2
    exit 1
fi
if [[ ! -f "$STT_MODEL" ]]; then
    echo "[$STT_LABEL] ERREUR : GGUF absent ($STT_MODEL) — page « Modèles » ou hf download." >&2
    exit 1
fi

STT_SERVE_CMD=("$PARAKEET_HOME/bin/parakeet-server" --model "$STT_MODEL" --host "$STT_HOST" --port "$STT_PORT")
STT_RESERVE_PORTS=("$STT_PORT")

stt_serve

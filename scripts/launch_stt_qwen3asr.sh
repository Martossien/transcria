#!/usr/bin/env bash
# Lance le runtime STT servi audio.cpp avec Qwen3-ASR-1.7B (backend `qwen3asr`),
# API compatible OpenAI (/v1/audio/transcriptions, accepte `language`).
#
# PRÉREQUIS (une fois) :
#   venv/bin/python -m transcria.installer.cli audiocpp --with-model
#   → runtimes/audiocpp/{bin/audiocpp_server, venv/, src/, models/Qwen3-ASR-1.7B-hf}
#
# USAGE
#   ./scripts/launch_stt_qwen3asr.sh
#   STT_GPU=0 STT_PORT=8021 ./scripts/launch_stt_qwen3asr.sh
#   scripts/stop_stt.sh --port 8021                        # pour arrêter
#
# PARAMÈTRES (variables d'env, défauts)
#   STT_GPU=0            GPU dédié (CUDA_VISIBLE_DEVICES — le JSON serveur vise
#                        toujours device 0, index RELATIF au masque)
#   STT_PORT=8021        port HTTP (hors llm_cleanup_ports)
#   STT_MODEL            chemin du modèle (défaut: runtimes/audiocpp/src/models/Qwen3-ASR-1.7B-hf)
#   STT_SERVED_NAME=qwen3-asr-1.7b   id servi (doit matcher inference.stt.backends.qwen3asr.model)
#
# BON À SAVOIR
#   - audio.cpp ne gère PAS le MP3 en entrée : TranscrIA envoie toujours du WAV
#     16 kHz mono (RemoteTranscriber._materialize_wav) — rien à faire.
#   - STT_GPU_MEM est IGNORÉ par audiocpp_server : la fraction du manifeste
#     resource_node.engines ne sert qu'à l'admission VRAM (calibrer sur la conso
#     réelle ; repère RTX 3090 : ~4-5 GiB pour le 1.7B, fenêtre 5 min ~10-14 s).
#   - Qualifié sur notre benchmark de réunions réelles : WER 0,421 (2ᵉ du banc) —
#     cf. docs/STT_BENCHMARK_REAL_MEETINGS.md. Commit épinglé : voir
#     transcria/installer/audiocpp_phase.py (AUDIOCPP_PINNED_COMMIT).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

RUNTIMES_DIR="${TRANSCRIA_RUNTIMES_DIR:-$REPO_ROOT/runtimes}"
AUDIOCPP_HOME="$RUNTIMES_DIR/audiocpp"

STT_LABEL="stt-qwen3asr"
STT_ENGINE="custom"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 0)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8021)"
STT_HOST="${STT_HOST:-0.0.0.0}"
STT_MODEL="${STT_MODEL:-$AUDIOCPP_HOME/src/models/Qwen3-ASR-1.7B-hf}"
STT_SERVED_NAME="${STT_SERVED_NAME:-qwen3-asr-1.7b}"
# Famille du loader audio.cpp — permet de servir un AUTRE modèle du même runtime :
#   STT_FAMILY=nemotron_asr STT_MODEL=…/nemotron-3.5-asr-streaming-0.6b \
#     STT_SERVED_NAME=nemotron STT_PORT=8023 ./scripts/launch_stt_qwen3asr.sh
STT_FAMILY="${STT_FAMILY:-qwen3_asr}"

if [[ ! -x "$AUDIOCPP_HOME/bin/audiocpp_server" ]]; then
    echo "[$STT_LABEL] ERREUR : $AUDIOCPP_HOME/bin/audiocpp_server absent." >&2
    echo "[$STT_LABEL] Provisionner : venv/bin/python -m transcria.installer.cli audiocpp --with-model" >&2
    exit 1
fi
if [[ ! -e "$STT_MODEL" ]]; then
    echo "[$STT_LABEL] ERREUR : modèle absent ($STT_MODEL)." >&2
    echo "[$STT_LABEL] Télécharger : … installer.cli audiocpp --with-model (ou page « Modèles »)." >&2
    exit 1
fi

# Config serveur générée par le helper Python TESTABLE (pas de heredoc JSON bash).
CFG_JSON="$AUDIOCPP_HOME/etc/server_${STT_PORT}.json"
mkdir -p "$AUDIOCPP_HOME/etc"
if ! "$REPO_ROOT/venv/bin/python" -m transcria.installer.audiocpp_phase \
    --emit-config --port "$STT_PORT" --host "$STT_HOST" \
    --model-id "$STT_SERVED_NAME" --model-path "$STT_MODEL" \
    --family "$STT_FAMILY" > "$CFG_JSON"; then
    echo "[$STT_LABEL] ERREUR : émission de la config serveur échouée (venv/import ?)." >&2
    rm -f "$CFG_JSON"
    exit 1
fi

STT_SERVE_CMD=("$AUDIOCPP_HOME/bin/audiocpp_server" --config "$CFG_JSON")
STT_RESERVE_PORTS=("$STT_PORT")

stt_serve

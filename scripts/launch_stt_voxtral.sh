#!/usr/bin/env bash
# Lance le runtime STT servi audio.cpp avec Voxtral-Mini-4B-Realtime (backend
# `voxtralrt`), API compatible OpenAI (/v1/audio/transcriptions, accepte `language`).
#
# Voxtral est un modèle Mistral servi par audio.cpp en GGUF Q8_0 (famille
# `voxtral_realtime`, ajoutée upstream au commit 6313916). MÊME runtime/binaire
# que qwen3asr — seuls la famille du loader et le modèle changent.
#
# PRÉREQUIS (une fois) :
#   venv/bin/python -m transcria.installer.cli audiocpp        # runtime + binaire
#   → puis le GGUF via la page « Modèles » (paquet voxtral_realtime) ou :
#     runtimes/audiocpp/venv/bin/python runtimes/audiocpp/src/tools/model_manager.py \
#       install voxtral_realtime   (cwd = runtimes/audiocpp/src)
#
# USAGE
#   ./scripts/launch_stt_voxtral.sh
#   STT_GPU=0 STT_PORT=8024 ./scripts/launch_stt_voxtral.sh
#   scripts/stop_stt.sh --port 8024                        # pour arrêter
#
# PARAMÈTRES (variables d'env, défauts)
#   STT_GPU=0            GPU dédié (CUDA_VISIBLE_DEVICES — le JSON serveur vise
#                        toujours device 0, index RELATIF au masque)
#   STT_PORT=8024        port HTTP (hors llm_cleanup_ports ; distinct de qwen3asr)
#   STT_MODEL            chemin du GGUF (défaut: runtimes/audiocpp/models/
#                        Voxtral-Mini-4B-Realtime-2602-GGUF/…q8_0.gguf)
#   STT_SERVED_NAME=voxtral-mini-4b-rt   id servi (doit matcher inference.stt.backends.voxtralrt.model)
#
# BON À SAVOIR
#   - audio.cpp ne gère PAS le MP3 : TranscrIA envoie toujours du WAV 16 kHz mono
#     (RemoteTranscriber._materialize_wav) — rien à faire.
#   - STT_GPU_MEM est IGNORÉ par audiocpp_server : la fraction du manifeste
#     resource_node.engines ne sert qu'à l'admission VRAM (calibrer sur la conso
#     réelle ; repère : ~5-6 GiB pour le 4B Q8_0).
#   - Binaire compilé avec AUDIOCPP_DEPLOYMENT_BUILD=ON (specs embarqués) — cf.
#     transcria/installer/audiocpp_phase.py (AUDIOCPP_PINNED_COMMIT).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/_stt_serve_lib.sh"

RUNTIMES_DIR="${TRANSCRIA_RUNTIMES_DIR:-$REPO_ROOT/runtimes}"
AUDIOCPP_HOME="$RUNTIMES_DIR/audiocpp"

STT_LABEL="stt-voxtralrt"
STT_ENGINE="custom"
STT_GPU="$(_stt_default STT_GPU VLLM_GPU 0)"
STT_PORT="$(_stt_default STT_PORT VLLM_PORT 8024)"
STT_HOST="${STT_HOST:-0.0.0.0}"
STT_MODEL="${STT_MODEL:-$AUDIOCPP_HOME/src/models/Voxtral-Mini-4B-Realtime-2602-GGUF/voxtral-mini-4b-realtime-2602-q8_0.gguf}"
STT_SERVED_NAME="${STT_SERVED_NAME:-voxtral-mini-4b-rt}"
STT_FAMILY="${STT_FAMILY:-voxtral_realtime}"

if [[ ! -x "$AUDIOCPP_HOME/bin/audiocpp_server" ]]; then
    echo "[$STT_LABEL] ERREUR : $AUDIOCPP_HOME/bin/audiocpp_server absent." >&2
    echo "[$STT_LABEL] Provisionner : venv/bin/python -m transcria.installer.cli audiocpp" >&2
    exit 1
fi
if [[ ! -e "$STT_MODEL" ]]; then
    echo "[$STT_LABEL] ERREUR : modèle absent ($STT_MODEL)." >&2
    echo "[$STT_LABEL] Télécharger : paquet voxtral_realtime (model_manager) ou page « Modèles »." >&2
    exit 1
fi

# Config serveur générée par le helper Python TESTABLE (pas de heredoc JSON bash).
CFG_JSON="$AUDIOCPP_HOME/etc/server_${STT_PORT}.json"
mkdir -p "$AUDIOCPP_HOME/etc"
"$REPO_ROOT/venv/bin/python" -m transcria.installer.audiocpp_phase \
    --emit-config --port "$STT_PORT" --host "$STT_HOST" \
    --model-id "$STT_SERVED_NAME" --model-path "$STT_MODEL" \
    --family "$STT_FAMILY" > "$CFG_JSON"

STT_SERVE_CMD=("$AUDIOCPP_HOME/bin/audiocpp_server" --config "$CFG_JSON")
STT_RESERVE_PORTS=("$STT_PORT")

stt_serve

#!/bin/bash
# Petit test de fonctionnement d'un serveur vLLM STT.
#
# 1. Vérifie /v1/models (serveur prêt, modèle servi).
# 2. ASR (cohere/whisper) → POST /v1/audio/transcriptions sur un court extrait.
#    OMNI (granite)        → POST /v1/chat/completions avec l'audio en entrée.
#
# Usage :
#   scripts/test_vllm_stt.sh <port> [audio] [kind] [model_name]
#     kind   : asr (défaut) | omni
#   Exemples :
#     scripts/test_vllm_stt.sh 8001                       # Cohere (ASR)
#     scripts/test_vllm_stt.sh 8005 tests/test2.mp3       # Whisper (ASR)
#     scripts/test_vllm_stt.sh 8006 tests/test2.mp3 omni granite-speech
set -euo pipefail

PORT="${1:-8001}"
AUDIO="${2:-tests/test2.mp3}"
KIND="${3:-asr}"
MODEL="${4:-}"
BASE="http://127.0.0.1:${PORT}"

echo "== Test vLLM STT — ${BASE} (kind=${KIND}) =="

# 1. /v1/models
echo "-- /v1/models --"
MODELS_JSON="$(curl -fsS --max-time 10 "${BASE}/v1/models")" || {
  echo "ÉCHEC : serveur injoignable sur ${BASE} (pas démarré ?)" >&2
  exit 1
}
echo "${MODELS_JSON}"
# Déduire le nom du modèle servi si non fourni.
if [ -z "${MODEL}" ]; then
  MODEL="$(printf '%s' "${MODELS_JSON}" | grep -oE '"id"[ ]*:[ ]*"[^"]+"' | head -1 | sed -E 's/.*"id"[ ]*:[ ]*"([^"]+)".*/\1/')"
fi
echo "Modèle servi : ${MODEL}"

if [ ! -f "${AUDIO}" ]; then
  echo "ÉCHEC : audio introuvable : ${AUDIO}" >&2
  exit 1
fi

# 2. Inférence
echo "-- inférence (${KIND}) --"
if [ "${KIND}" = "omni" ]; then
  # Granite : audio-in via chat. Encodage base64 de l'audio.
  B64="$(base64 -w0 "${AUDIO}")"
  PAYLOAD="$(printf '{"model":"%s","messages":[{"role":"user","content":[{"type":"text","text":"Transcris cet audio."},{"type":"audio_url","audio_url":{"url":"data:audio/mpeg;base64,%s"}}]}],"max_tokens":256}' "${MODEL}" "${B64}")"
  curl -fsS --max-time 120 "${BASE}/v1/chat/completions" \
    -H "Content-Type: application/json" -d "${PAYLOAD}" | head -c 2000
else
  # Cohere / Whisper : ASR dédié.
  curl -fsS --max-time 120 "${BASE}/v1/audio/transcriptions" \
    -F "file=@${AUDIO}" -F "model=${MODEL}" -F "language=fr" | head -c 2000
fi
echo
echo "== OK : ${BASE} répond et a produit une sortie =="

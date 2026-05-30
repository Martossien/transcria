#!/bin/bash
# Petit test de fonctionnement d'un serveur STT compatible OpenAI (vLLM, SGLang, …).
#
# 1. Vérifie /v1/models (serveur prêt, modèle servi).
# 2. ASR (cohere/whisper) → POST /v1/audio/transcriptions sur un court extrait.
#    OMNI (granite)        → POST /v1/chat/completions avec l'audio en entrée.
#
# BUG MP3 (observé sur vLLM) : l'endpoint /v1/audio/transcriptions rejette les
# fichiers MP3 (erreur 400 « Invalid or unsupported audio file »). Cause : soundfile
# échoue sur MP3 en flux BytesIO avec un code non couvert par le fallback vers PyAV
# (_BAD_SF_CODES = {0,1,3,4}). WAV et OGG fonctionnent.
# → Ce script convertit automatiquement le fichier en WAV 16kHz mono avant l'envoi
#   si l'extension est .mp3.
#
# Usage :
#   scripts/test_stt.sh <port> [audio] [kind] [model_name]
#     kind   : asr (défaut) | omni
#   Exemples :
#     scripts/test_stt.sh 8003                       # Cohere (ASR)
#     scripts/test_stt.sh 8005 tests/test2.mp3       # Whisper (ASR, auto-converti en WAV)
#     scripts/test_stt.sh 8007 tests/test2.mp3 omni granite-speech
set -euo pipefail

PORT="${1:-8003}"
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

# Conversion MP3 → WAV 16kHz mono si nécessaire (bug vLLM : MP3 rejeté sur l'endpoint ASR)
SEND_FILE="${AUDIO}"
if [[ "${AUDIO,,}" == *.mp3 ]]; then
  SEND_FILE="$(mktemp /tmp/vllm_test_XXXXXX.wav)"
  echo "-- conversion MP3 → WAV 16kHz mono (bug vLLM MP3) --"
  ffmpeg -y -i "${AUDIO}" -ar 16000 -ac 1 -f wav "${SEND_FILE}" 2>/dev/null
  echo "Fichier converti : ${SEND_FILE} ($(stat -c%s "${SEND_FILE}") octets)"
fi

# Omni utilise le fichier original (base64), pas le WAV converti
OMNI_FILE="${AUDIO}"

# 2. Inférence
echo "-- inférence (${KIND}) --"
if [ "${KIND}" = "omni" ]; then
  # Granite : audio-in via chat. Encodage base64 de l'audio.
  B64="$(base64 -w0 "${OMNI_FILE}")"
  PAYLOAD="$(printf '{"model":"%s","messages":[{"role":"user","content":[{"type":"text","text":"Transcris cet audio."},{"type":"audio_url","audio_url":{"url":"data:audio/mpeg;base64,%s"}}]}],"max_tokens":256}' "${MODEL}" "${B64}")"
  curl -fsS --max-time 120 "${BASE}/v1/chat/completions" \
    -H "Content-Type: application/json" -d "${PAYLOAD}" | head -c 2000
else
  # Cohere / Whisper : ASR dédié.
  curl -fsS --max-time 120 "${BASE}/v1/audio/transcriptions" \
    -F "file=@${SEND_FILE}" -F "model=${MODEL}" -F "language=fr" | head -c 2000
fi
echo

# Nettoyage du fichier temporaire si converti
if [[ "${SEND_FILE}" != "${AUDIO}" && -f "${SEND_FILE}" ]]; then
  rm -f "${SEND_FILE}"
fi

echo "== OK : ${BASE} répond et a produit une sortie =="

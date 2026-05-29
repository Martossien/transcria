"""POST /infer/diarize — diarisation (tours de parole) depuis un audio.

Mêmes deux transports que voice-embed (docs §4bis.2) :
  - **référence fichier** : JSON {"audio_path": "/chemin/audio.wav"} (+ options)
  - **upload** : multipart file=<audio> (+ champs num_speakers/min/max optionnels)

Réponse = dict canonique de diarisation : available, turns, exclusive_turns,
speakers, stats. Format identique à `speakers/speaker_turns.json` du pipeline,
donc directement consommable par un `RemoteDiarizer` côté frontend.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from inference_service.errors import BadRequestError

logger = logging.getLogger("inference_service.diarize")

diarize_bp = Blueprint("diarize", __name__)

_ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4"}


def _engine():
    return current_app.extensions["diarize_engine"]


@diarize_bp.route("/infer/diarize", methods=["POST"])
def diarize():
    engine = _engine()
    content_type = request.content_type or ""
    if content_type.startswith("multipart/form-data"):
        result = _handle_upload(engine)
    else:
        result = _handle_file_ref(engine)
    return jsonify(result), 200


def _handle_file_ref(engine) -> dict:
    data = request.get_json(silent=True) or {}
    raw_path = data.get("audio_path")
    if not raw_path or not isinstance(raw_path, str):
        raise BadRequestError("champ 'audio_path' requis (ou utilisez un upload multipart)")
    audio_path = Path(raw_path)
    if not audio_path.is_file():
        raise BadRequestError(f"fichier introuvable: {raw_path}", code="audio_not_found")
    logger.info("diarize (file_ref) | path=%s", audio_path)
    return engine.diarize(audio_path)


def _handle_upload(engine) -> dict:
    file = request.files.get("file")
    if file is None or not file.filename:
        raise BadRequestError("champ multipart 'file' requis")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise BadRequestError(f"extension non supportée: {suffix or '(aucune)'}", code="unsupported_format")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        file.save(tmp.name)
        tmp.flush()
        logger.info("diarize (upload) | filename=%s suffix=%s", file.filename, suffix)
        return engine.diarize(Path(tmp.name))

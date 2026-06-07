"""POST /infer/diarize — diarisation (tours de parole) depuis un audio.

Mêmes deux transports que voice-embed (docs §4bis.2) :
  - **référence fichier** : JSON {"audio_path": "/chemin/audio.wav"} (+ options)
  - **upload** : multipart file=<audio> (+ champs num_speakers/min/max optionnels)

Contrainte de locuteurs **par appel** (optionnelle) : `num_speakers`, `min_speakers`,
`max_speakers` (entiers ≥ 1), en JSON ou champs de formulaire. Transmise au moteur, elle
prime sur la config statique du nœud — parité avec la fourchette de locuteurs par job du
mode local. Valeurs invalides ignorées (re-validées au moteur via `_normalize_speaker_params`).

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
from inference_service.security import resolve_safe_audio_path

logger = logging.getLogger("inference_service.diarize")

diarize_bp = Blueprint("diarize", __name__)

_ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4"}
_SPEAKER_KEYS = ("num_speakers", "min_speakers", "max_speakers")


def _engine():
    return current_app.extensions["diarize_engine"]


def _parse_speaker_params(source) -> dict | None:
    """Extrait num/min/max_speakers d'un mapping (JSON ou form), entiers tolérants.

    Retourne `None` si aucune contrainte exploitable (le moteur retombe alors sur sa
    config). La validation fine (≥ 1, types) est refaite au moteur — ici on se contente
    de convertir proprement les chaînes de formulaire en entiers.
    """
    if not source:
        return None
    params: dict[str, int] = {}
    for key in _SPEAKER_KEYS:
        raw = source.get(key)
        if raw is None or raw == "":
            continue
        try:
            params[key] = int(raw)
        except (TypeError, ValueError):
            logger.warning("diarize: %s ignoré (entier attendu) : %r", key, raw)
    return params or None


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
    config = current_app.config["TRANSCRIA_CONFIG"]
    # Anti-traversal : refuse tout chemin hors des racines autorisées (403).
    audio_path = resolve_safe_audio_path(raw_path, config)
    if not audio_path.is_file():
        raise BadRequestError(f"fichier introuvable: {raw_path}", code="audio_not_found")
    speaker_params = _parse_speaker_params(data)
    logger.info("diarize (file_ref) | path=%s speaker_params=%s", audio_path, speaker_params)
    return engine.diarize(audio_path, speaker_params=speaker_params)


def _handle_upload(engine) -> dict:
    file = request.files.get("file")
    if file is None or not file.filename:
        raise BadRequestError("champ multipart 'file' requis")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise BadRequestError(f"extension non supportée: {suffix or '(aucune)'}", code="unsupported_format")
    speaker_params = _parse_speaker_params(request.form)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        file.save(tmp.name)
        tmp.flush()
        logger.info("diarize (upload) | filename=%s suffix=%s speaker_params=%s", file.filename, suffix, speaker_params)
        return engine.diarize(Path(tmp.name), speaker_params=speaker_params)

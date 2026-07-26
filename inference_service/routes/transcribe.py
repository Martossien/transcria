"""POST /infer/transcribe — transcription (STT) d'un extrait audio sur le nœud de calcul.

Pourquoi cet endpoint : la façade `POST /v1/audio/transcriptions` transcrivait DANS LE
PROCESS WEB. Conséquences mesurées : le modèle reste résident et occupe la VRAM de la
frontale (au détriment de la file de traitement), et en déploiement SPLIT une frontale
`role=web` sans GPU ne peut tout simplement pas répondre. L'inférence appartient donc au
nœud de ressources, comme la diarisation et les embeddings voix.

Mêmes deux transports que `/infer/diarize` (docs §4bis.2) :
  - **référence fichier** : JSON {"audio_path": "/chemin/audio.wav"} (+ options)
  - **upload** : multipart file=<audio> (+ champs `language`, `backend` optionnels)

Réponse : `{"segments": [...], "text": "...", "backend": "..."}` — les segments gardent la
forme du pipeline (start/end/text), directement consommables par la façade.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from inference_service.errors import BadRequestError
from inference_service.security import resolve_safe_audio_path

logger = logging.getLogger("inference_service.transcribe")

transcribe_bp = Blueprint("transcribe", __name__)

_ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".webm"}


def _options(source) -> tuple[str | None, str | None]:
    """(langue, backend) depuis un mapping JSON ou un formulaire. Vides ⇒ défauts du nœud."""
    if not source:
        return None, None
    language = (source.get("language") or "").strip() or None
    backend = (source.get("backend") or "").strip() or None
    return language, backend


def _engine():
    return current_app.extensions["transcribe_engine"]


def _transcribe(audio_path: Path, language: str | None, backend: str | None) -> dict:
    """Transcrit un fichier via le moteur RÉSIDENT du nœud (modèle chargé une fois,
    accès sérialisé) et rend un dict sérialisable."""
    segments = _engine().transcribe(audio_path, language=language or "fr", backend=backend)
    # Le pipeline manipule des segments DICT (cf. facade_format / stamp_provenance) :
    # on les renvoie tels quels, en normalisant seulement les types.
    items: list[dict] = [
        {
            "start": float(seg.get("start") or 0.0),
            "end": float(seg.get("end") or 0.0),
            "text": str(seg.get("text") or ""),
        }
        for seg in (segments or []) if isinstance(seg, dict)
    ]
    return {
        "segments": items,
        "text": " ".join(str(item["text"]).strip() for item in items
                          if str(item["text"]).strip()),
        "backend": backend or "",
    }


@transcribe_bp.route("/infer/transcribe", methods=["POST"])
def transcribe():
    """Transcrit un audio sur le nœud de calcul (référence fichier ou upload multipart)."""
    content_type = request.content_type or ""
    if content_type.startswith("multipart/form-data"):
        return jsonify(_handle_upload()), 200
    return jsonify(_handle_file_ref()), 200


def _handle_file_ref() -> dict:
    data = request.get_json(silent=True) or {}
    raw_path = data.get("audio_path")
    if not raw_path or not isinstance(raw_path, str):
        raise BadRequestError("champ 'audio_path' requis (ou utilisez un upload multipart)")
    config = current_app.config["TRANSCRIA_CONFIG"]
    # Anti-traversal : refuse tout chemin hors des racines autorisées (403).
    audio_path = resolve_safe_audio_path(raw_path, config)
    if not audio_path.is_file():
        raise BadRequestError(f"fichier introuvable: {raw_path}", code="audio_not_found")
    language, backend = _options(data)
    logger.info("transcribe (file_ref) | path=%s langue=%s backend=%s",
                audio_path, language, backend)
    return _transcribe(audio_path, language, backend)


def _handle_upload() -> dict:
    file = request.files.get("file")
    if file is None or not file.filename:
        raise BadRequestError("champ multipart 'file' requis")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise BadRequestError(f"extension non supportée: {suffix or '(aucune)'}",
                              code="unsupported_format")
    language, backend = _options(request.form)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        file.save(tmp.name)
        tmp.flush()
        logger.info("transcribe (upload) | filename=%s langue=%s backend=%s",
                    file.filename, language, backend)
        return _transcribe(Path(tmp.name), language, backend)

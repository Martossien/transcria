"""POST /infer/voice-embed — produit une empreinte vocale depuis un audio.

Deux transports supportés dès la v1 (docs §4bis.2), pour que le passage
mono-machine → distant ne change que l'URL :
  - **référence fichier** (mono-machine, même filesystem) :
        Content-Type: application/json   body: {"audio_path": "/chemin/ref.wav"}
  - **upload** (distant / frontal séparé) :
        Content-Type: multipart/form-data   field: file=<audio>

Réponse : métadonnées + vecteur normalisé L2 encodé en base64 (blob float32
little-endian), reconstruisible côté client via deserialize_embedding(blob, dim).
"""
from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from inference_service.errors import BadRequestError
from transcria.voice.embedding import VoiceEmbedding, serialize_embedding

logger = logging.getLogger("inference_service.voice_embed")

voice_embed_bp = Blueprint("voice_embed", __name__)

_ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4"}


def _engine():
    return current_app.extensions["voice_engine"]


def _embedding_payload(embedding: VoiceEmbedding) -> dict:
    blob = serialize_embedding(embedding.vector)
    return {
        "backend": embedding.backend,
        "model_id": embedding.model_id,
        "model_revision": embedding.model_revision,
        "normalization": embedding.normalization,
        "dim": embedding.dim,
        "sample_count": embedding.sample_count,
        "speech_duration_s": round(embedding.speech_duration_s, 3),
        "quality_status": embedding.quality_status,
        "sha256": embedding.sha256,
        # Vecteur exact (pas de perte de précision JSON) : blob f4 LE en base64.
        "vector_b64": base64.b64encode(blob).decode("ascii"),
    }


@voice_embed_bp.route("/infer/voice-embed", methods=["POST"])
def voice_embed():
    engine = _engine()
    content_type = request.content_type or ""

    if content_type.startswith("multipart/form-data"):
        embedding = _handle_upload(engine)
    else:
        embedding = _handle_file_ref(engine)

    return jsonify(_embedding_payload(embedding)), 200


def _handle_file_ref(engine) -> VoiceEmbedding:
    """Transport mono-machine : le service lit directement le fichier référencé."""
    data = request.get_json(silent=True) or {}
    raw_path = data.get("audio_path")
    if not raw_path or not isinstance(raw_path, str):
        raise BadRequestError("champ 'audio_path' requis (ou utilisez un upload multipart)")
    audio_path = Path(raw_path)
    if not audio_path.is_file():
        raise BadRequestError(f"fichier introuvable: {raw_path}", code="audio_not_found")
    logger.info("voice-embed (file_ref) | path=%s", audio_path)
    return engine.extract(audio_path)


def _handle_upload(engine) -> VoiceEmbedding:
    """Transport distant : l'audio est uploadé, écrit en temporaire puis traité."""
    file = request.files.get("file")
    if file is None or not file.filename:
        raise BadRequestError("champ multipart 'file' requis")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise BadRequestError(f"extension non supportée: {suffix or '(aucune)'}", code="unsupported_format")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        file.save(tmp.name)
        tmp.flush()
        logger.info("voice-embed (upload) | filename=%s suffix=%s", file.filename, suffix)
        return engine.extract(Path(tmp.name))

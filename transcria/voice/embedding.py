from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_VERSION = "v1"


class VoiceEmbeddingError(RuntimeError):
    """Erreur contrôlée pendant la génération d'une empreinte vocale."""


@dataclass(frozen=True)
class VoiceEmbedding:
    vector: np.ndarray
    backend: str
    model_id: str
    model_revision: str
    normalization: str
    sample_count: int
    speech_duration_s: float
    quality_status: str = "ok"

    @property
    def dim(self) -> int:
        return int(self.vector.shape[0])

    @property
    def sha256(self) -> str:
        return hashlib.sha256(serialize_embedding(self.vector)).hexdigest()


def normalize_l2(vector: np.ndarray) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.size == 0 or not np.all(np.isfinite(values)):
        raise VoiceEmbeddingError("embedding_invalide")
    norm = float(np.linalg.norm(values))
    if norm <= 0:
        raise VoiceEmbeddingError("embedding_norme_nulle")
    return (values / norm).astype(np.float32)


def serialize_embedding(vector: np.ndarray) -> bytes:
    return normalize_l2(vector).astype("<f4").tobytes()


def deserialize_embedding(blob: bytes, dim: int) -> np.ndarray:
    values = np.frombuffer(blob, dtype="<f4")
    if values.size != dim:
        raise VoiceEmbeddingError("dimension_embedding_invalide")
    return normalize_l2(values)


def cosine_raw(left: np.ndarray, right: np.ndarray) -> float:
    a = normalize_l2(left)
    b = normalize_l2(right)
    return float(np.dot(a, b))


class PyannoteVoiceEmbeddingBackend:
    """Extraction d'empreinte vocale depuis le pipeline pyannote déjà utilisé."""

    backend_name = "pyannote"

    def __init__(self, config: dict, device: str = "cpu") -> None:
        self.config = config
        self.device = device
        embedding_cfg = config.get("voice_enrollment", {}).get("embedding", {})
        model_cfg = config.get("models", {})
        self.model_id = embedding_cfg.get("model_id") or model_cfg.get("pyannote_model", "pyannote/speaker-diarization-community-1")
        self.model_revision = embedding_cfg.get("model_revision") or ""

    def extract_reference_embedding(self, audio_path: Path) -> VoiceEmbedding:
        if not audio_path.is_file():
            raise VoiceEmbeddingError("audio_reference_introuvable")
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise VoiceEmbeddingError("pyannote_indisponible") from exc

        logger.info("Chargement pyannote pour empreinte vocale: model=%s device=%s", self.model_id, self.device)
        pipeline = Pipeline.from_pretrained(self.model_id)
        try:
            pipeline.to(torch.device(self.device))
        except Exception as exc:
            logger.warning("pyannote empreinte vocale: device %s ignoré (%s)", self.device, exc)

        output = pipeline(str(audio_path))
        embeddings = getattr(output, "speaker_embeddings", None)
        if embeddings is None:
            raise VoiceEmbeddingError("speaker_embeddings_absents")

        arr = np.asarray(embeddings, dtype=np.float32)
        if arr.ndim == 3:
            arr = arr.reshape((-1, arr.shape[-1]))
        if arr.ndim != 2 or arr.shape[0] == 0:
            raise VoiceEmbeddingError("speaker_embeddings_format_invalide")

        valid = arr[np.all(np.isfinite(arr), axis=1)]
        if valid.size == 0:
            raise VoiceEmbeddingError("speaker_embeddings_vides")

        quality_status = "ok" if valid.shape[0] == 1 else "multiple_speakers_reference"
        vector = normalize_l2(np.mean([normalize_l2(row) for row in valid], axis=0))
        duration_s = _duration_from_output(output)
        logger.info(
            "Empreinte vocale générée: backend=%s model=%s dim=%d samples=%d quality=%s",
            self.backend_name,
            self.model_id,
            vector.shape[0],
            valid.shape[0],
            quality_status,
        )
        return VoiceEmbedding(
            vector=vector,
            backend=self.backend_name,
            model_id=self.model_id,
            model_revision=self.model_revision,
            normalization="l2",
            sample_count=int(valid.shape[0]),
            speech_duration_s=duration_s,
            quality_status=quality_status,
        )


def _duration_from_output(output) -> float:
    diarization = getattr(output, "speaker_diarization", None)
    if diarization is None:
        return 0.0
    try:
        return float(sum(segment.duration for segment, _, _ in diarization.itertracks(yield_label=True)))
    except Exception:
        return 0.0

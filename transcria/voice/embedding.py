from __future__ import annotations

import base64
import hashlib
import logging
import os
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

        hf_token = os.environ.get("HF_TOKEN") or None
        logger.info("Chargement pyannote pour empreinte vocale: model=%s device=%s token=%s",
                    self.model_id, self.device, "oui" if hf_token else "non")
        pipeline = Pipeline.from_pretrained(self.model_id, token=hf_token)
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


class RemoteVoiceEmbeddingBackend:
    """Empreinte vocale déléguée au service d'inférence distant.

    Même interface que `PyannoteVoiceEmbeddingBackend` (`extract_reference_embedding`).
    Reconstruit le `VoiceEmbedding` depuis le payload du service et vérifie son
    intégrité (sha256). Bascule sur le backend local si le service est indisponible
    et que `fallback_local` est activé.
    """

    backend_name = "remote"

    def __init__(self, config: dict, device: str = "cpu", client=None) -> None:
        self.config = config
        self.device = device
        inf = config.get("inference", {}) or {}
        self.fallback_local: bool = bool(
            (inf.get("voice_embed", {}) or {}).get("fallback_local", inf.get("fallback_local", True))
        )
        if client is None:
            from transcria.inference.client import build_client_from_config
            client = build_client_from_config(config)
        self._client = client

    def extract_reference_embedding(self, audio_path: Path) -> VoiceEmbedding:
        from transcria.inference.client import InferenceRequestError, InferenceUnavailable

        if self._client is None:
            return self._fallback_or_raise(audio_path, "aucun client distant configuré")
        try:
            payload = self._client.voice_embed(audio_path)
        except InferenceUnavailable as exc:
            logger.warning("Empreinte vocale (remote): service indisponible — %s", exc)
            return self._fallback_or_raise(audio_path, str(exc))
        except InferenceRequestError as exc:
            # 4xx métier : l'audio est en cause, le fallback échouerait pareil.
            raise VoiceEmbeddingError(f"service_embedding: {exc.code or exc}") from exc
        return self._payload_to_embedding(payload)

    def _payload_to_embedding(self, payload: dict) -> VoiceEmbedding:
        try:
            dim = int(payload["dim"])
            blob = base64.b64decode(payload["vector_b64"])
            vector = deserialize_embedding(blob, dim)
        except (KeyError, TypeError, ValueError, VoiceEmbeddingError) as exc:
            raise VoiceEmbeddingError(f"reponse_embedding_invalide: {exc}") from exc

        embedding = VoiceEmbedding(
            vector=vector,
            backend=str(payload.get("backend", "remote")),
            model_id=str(payload.get("model_id", "")),
            model_revision=str(payload.get("model_revision", "")),
            normalization=str(payload.get("normalization", "l2")),
            sample_count=int(payload.get("sample_count", 1)),
            speech_duration_s=float(payload.get("speech_duration_s", 0.0)),
            quality_status=str(payload.get("quality_status", "ok")),
        )
        expected = payload.get("sha256")
        if expected and embedding.sha256 != expected:
            raise VoiceEmbeddingError("embedding_corrompu (sha256 ne correspond pas au payload)")
        logger.info(
            "Empreinte vocale (remote) reçue: dim=%d samples=%d quality=%s",
            embedding.dim, embedding.sample_count, embedding.quality_status,
        )
        return embedding

    def _fallback_or_raise(self, audio_path: Path, reason: str) -> VoiceEmbedding:
        if self.fallback_local:
            logger.warning("Empreinte vocale (remote): bascule sur le backend local (%s)", reason)
            return PyannoteVoiceEmbeddingBackend(self.config, device=self.device).extract_reference_embedding(audio_path)
        raise VoiceEmbeddingError(f"service_embedding_indisponible: {reason}")


def create_voice_embedding_backend(config: dict, device: str = "cpu"):
    """Sélectionne le backend d'empreinte vocale selon la config `inference`.

    Distant si une URL est configurée ET `inference.mode` ∈ {remote, hybrid}.
    Sinon, backend pyannote local (comportement historique préservé).
    """
    inf = config.get("inference", {}) or {}
    url = inf.get("url") or inf.get("base_url")
    mode = inf.get("mode", "local")
    if url and mode in ("remote", "hybrid"):
        logger.info("Empreinte vocale : backend distant (%s, mode=%s)", url, mode)
        return RemoteVoiceEmbeddingBackend(config, device=device)
    return PyannoteVoiceEmbeddingBackend(config, device=device)

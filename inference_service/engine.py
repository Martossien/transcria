"""Moteur d'embedding vocal du service — gestion VRAM A/B/C + concurrence.

Logique transposée du LLM d'arbitrage (docs/MIGRATION_API_SERVEUR_GPU.md §4bis.3) :
  - CAS A : modèle déjà résident en VRAM            → sert directement
  - CAS B : modèle non chargé, VRAM libre           → charge puis sert
  - CAS C : VRAM occupée (OOM CUDA au chargement)   → GpuBusyError (503)

Le GPU est sérialisé par un verrou : une seule extraction à la fois (un service
d'inférence GPU-bound ne gagne rien à paralléliser sur une même carte). Le modèle
est résident avec idle-timeout : déchargé après N secondes sans requête.

Le backend pyannote est injectable (`backend_factory`) pour permettre les tests
sans GPU.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from inference_service.errors import GpuBusyError, UnprocessableError
from inference_service.load import SerializedLoadTracker
from transcria.voice.embedding import (
    PyannoteVoiceEmbeddingBackend,
    VoiceEmbedding,
    VoiceEmbeddingError,
)

logger = logging.getLogger("inference_service.engine")

# Signatures d'erreur indiquant une saturation VRAM côté CUDA/torch.
_OOM_MARKERS = ("out of memory", "cuda error", "cublas", "no kernel image", "alloc")


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _OOM_MARKERS)


class VoiceEmbedEngine:
    """Moteur résident, thread-safe, qui produit des empreintes vocales.

    Args:
        config: configuration TranscrIA (model_id, device, idle_timeout).
        backend_factory: fabrique le backend d'extraction. Par défaut pyannote ;
            injectable pour les tests (évite tout chargement GPU).
    """

    def __init__(
        self,
        config: dict,
        backend_factory: Callable[[], object] | None = None,
    ) -> None:
        self.config = config or {}
        voice_cfg = self.config.get("voice_enrollment", {}).get("embedding", {})
        self.device: str = voice_cfg.get("device", "cpu")
        self.idle_timeout_s: float = float(voice_cfg.get("idle_timeout_s", 300))
        self._backend_factory = backend_factory or self._default_backend_factory
        self._backend: object | None = None
        self._load = SerializedLoadTracker("voice-embed", logger)
        self._last_used: float = 0.0
        self.model_id: str = (
            voice_cfg.get("model_id")
            or self.config.get("models", {}).get("pyannote_model", "pyannote/speaker-diarization-community-1")
        )

    # ── Fabrique par défaut (pyannote, GPU) ───────────────────────────────────

    def _default_backend_factory(self) -> object:
        return PyannoteVoiceEmbeddingBackend(self.config, device=self.device)

    # ── État (pour /ready et /models) ─────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def status(self) -> dict:
        """État courant du moteur — alimente /ready et /models."""
        status = {
            "name": "voice-embed",
            "backend": "pyannote",
            "model_id": self.model_id,
            "device": self.device,
            "loaded": self.loaded,               # CAS A si True, CAS B sinon
            "idle_timeout_s": self.idle_timeout_s,
            "last_used_epoch": round(self._last_used, 3) if self._last_used else None,
        }
        status.update(self._load.snapshot())
        return status

    # ── Cycle de vie du modèle ────────────────────────────────────────────────

    def _ensure_loaded(self) -> object:
        """CAS A/B : retourne le backend résident, le charge si nécessaire.

        Lève GpuBusyError (CAS C) si le chargement échoue par saturation VRAM.
        """
        if self._backend is not None:
            return self._backend
        logger.info("Chargement du backend embedding (CAS B) | model=%s device=%s", self.model_id, self.device)
        try:
            self._backend = self._backend_factory()
        except Exception as exc:  # noqa: BLE001 — on classe l'erreur ci-dessous
            if _is_oom(exc):
                logger.warning("Chargement embedding refusé — VRAM saturée (CAS C) : %s", exc)
                raise GpuBusyError("VRAM saturée au chargement du modèle") from exc
            logger.exception("Échec chargement backend embedding")
            raise UnprocessableError(f"chargement_backend_impossible: {exc}") from exc
        return self._backend

    def unload(self) -> bool:
        """Décharge le modèle (libère la VRAM). Retourne True si un déchargement a eu lieu."""
        with self._load.acquire("unload"):
            if self._backend is None:
                return False
            logger.info("Déchargement du backend embedding (libération VRAM)")
            self._backend = None
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 — best effort
                pass
            return True

    def maybe_unload_if_idle(self) -> bool:
        """Décharge si le modèle est resté inactif au-delà de l'idle-timeout."""
        if self._backend is None or not self._last_used:
            return False
        if self.idle_timeout_s <= 0:
            return False
        if (time.monotonic() - self._last_used) >= self.idle_timeout_s:
            logger.info("Idle-timeout atteint (%.0fs) — déchargement", self.idle_timeout_s)
            return self.unload()
        return False

    # ── Extraction ────────────────────────────────────────────────────────────

    def extract(self, audio_path: Path) -> VoiceEmbedding:
        """Produit une empreinte vocale. Sérialisé par verrou (un GPU à la fois).

        Raises:
            GpuBusyError: VRAM saturée (CAS C) — le client doit re-planifier.
            UnprocessableError: échec métier (audio sans voix exploitable…).
        """
        started = time.monotonic()
        with self._load.acquire("extract"):
            backend = self._ensure_loaded()
            try:
                embedding = backend.extract_reference_embedding(audio_path)  # type: ignore[attr-defined]
            except VoiceEmbeddingError as exc:
                logger.warning("Embedding métier échoué: %s", exc)
                raise UnprocessableError(str(exc), code=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    logger.warning("Extraction refusée — VRAM saturée (CAS C) : %s", exc)
                    raise GpuBusyError("VRAM saturée pendant l'extraction") from exc
                logger.exception("Erreur inattendue pendant l'extraction")
                raise UnprocessableError(f"extraction_impossible: {exc}") from exc
            self._last_used = time.monotonic()
        logger.info(
            "Empreinte produite | dim=%d samples=%d quality=%s elapsed=%.2fs",
            embedding.dim, embedding.sample_count, embedding.quality_status,
            time.monotonic() - started,
        )
        return embedding

"""Moteur de diarisation du service — même patron VRAM A/B/C que l'embedding.

Réutilise `DiarizerService.diarize_audio()` (calcul pur, sans effet de bord
job/fs) extrait du pipeline. Le backend est injectable pour tester sans GPU.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from inference_service.errors import GpuBusyError, UnprocessableError

logger = logging.getLogger("inference_service.diarize")

_OOM_MARKERS = ("out of memory", "cuda error", "cublas", "no kernel image", "alloc")


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _OOM_MARKERS)


class DiarizeEngine:
    """Moteur résident, thread-safe, qui produit la diarisation d'un audio.

    Args:
        config: configuration TranscrIA (model, device, idle_timeout).
        backend_factory: fabrique le diariseur. Par défaut `DiarizerService` ;
            injectable pour les tests (évite tout chargement GPU). Le backend
            doit exposer `diarize_audio(audio_path) -> dict` et `model_name`.
    """

    def __init__(
        self,
        config: dict,
        backend_factory: Callable[[], object] | None = None,
    ) -> None:
        self.config = config or {}
        diar_cfg = self.config.get("diarization", {})
        self.device: str = diar_cfg.get("device", "cuda:0")
        self.idle_timeout_s: float = float(diar_cfg.get("idle_timeout_s", 300))
        self._backend_factory = backend_factory or self._default_backend_factory
        self._backend: object | None = None
        self._lock = threading.Lock()
        self._last_used: float = 0.0
        self.model_id: str = self.config.get("models", {}).get(
            "pyannote_model", "pyannote/speaker-diarization-community-1"
        )

    def _default_backend_factory(self) -> object:
        from transcria.stt.diarization import DiarizerService
        return DiarizerService(self.config, device=self.device)

    # ── État ──────────────────────────────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def status(self) -> dict:
        return {
            "name": "diarize",
            "backend": "pyannote",
            "model_id": self.model_id,
            "device": self.device,
            "loaded": self.loaded,
            "idle_timeout_s": self.idle_timeout_s,
            "last_used_epoch": round(self._last_used, 3) if self._last_used else None,
        }

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> object:
        if self._backend is not None:
            return self._backend
        logger.info("Chargement du diariseur (CAS B) | model=%s device=%s", self.model_id, self.device)
        try:
            self._backend = self._backend_factory()
        except Exception as exc:  # noqa: BLE001
            if _is_oom(exc):
                logger.warning("Chargement diariseur refusé — VRAM saturée (CAS C) : %s", exc)
                raise GpuBusyError("VRAM saturée au chargement du diariseur") from exc
            logger.exception("Échec chargement diariseur")
            raise UnprocessableError(f"chargement_diariseur_impossible: {exc}") from exc
        return self._backend

    def unload(self) -> bool:
        with self._lock:
            if self._backend is None:
                return False
            logger.info("Déchargement du diariseur (libération VRAM)")
            backend, self._backend = self._backend, None
            offload = getattr(backend, "offload", None)
            if callable(offload):
                try:
                    offload()
                except Exception:  # noqa: BLE001 — best effort
                    pass
            return True

    def maybe_unload_if_idle(self) -> bool:
        if self._backend is None or not self._last_used or self.idle_timeout_s <= 0:
            return False
        if (time.monotonic() - self._last_used) >= self.idle_timeout_s:
            logger.info("Idle-timeout atteint (%.0fs) — déchargement diariseur", self.idle_timeout_s)
            return self.unload()
        return False

    # ── Diarisation ─────────────────────────────────────────────────────────--

    def diarize(self, audio_path: Path) -> dict:
        """Diarise un audio. Sérialisé par verrou (un GPU à la fois).

        Retourne le dict canonique (`available`, `turns`, `exclusive_turns`,
        `speakers`, `stats`). Lève GpuBusyError (CAS C) si VRAM saturée.
        """
        started = time.monotonic()
        with self._lock:
            backend = self._ensure_loaded()
            try:
                result = backend.diarize_audio(audio_path)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    logger.warning("Diarisation refusée — VRAM saturée (CAS C) : %s", exc)
                    raise GpuBusyError("VRAM saturée pendant la diarisation") from exc
                logger.exception("Erreur inattendue pendant la diarisation")
                raise UnprocessableError(f"diarisation_impossible: {exc}") from exc
            self._last_used = time.monotonic()

        # diarize_audio peut signaler un échec métier via available=False + error
        if not result.get("available") and result.get("error"):
            err = str(result["error"])
            if _is_oom(Exception(err)):
                raise GpuBusyError("VRAM saturée pendant la diarisation")
            raise UnprocessableError(f"diarisation_echec: {err}", code="diarisation_echec")

        logger.info(
            "Diarisation produite | speakers=%d turns=%d elapsed=%.2fs",
            len(result.get("speakers") or []), len(result.get("turns") or []),
            time.monotonic() - started,
        )
        return result

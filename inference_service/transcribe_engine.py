"""Moteur STT du service — même patron résident/VRAM que la diarisation.

Sans ce moteur, chaque appel à `/infer/transcribe` reconstruirait un transcripteur : le
modèle serait rechargé à chaque requête et deux appels simultanés se marcheraient dessus sur
le GPU. On adopte donc le patron déjà en place pour la diarisation et les embeddings voix :
modèle RÉSIDENT, accès SÉRIALISÉ par verrou, déchargement après inactivité. Le backend est
injectable pour tester sans GPU.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from inference_service.errors import GpuBusyError, UnprocessableError
from inference_service.load import SerializedLoadTracker

logger = logging.getLogger("inference_service.transcribe")

_OOM_MARKERS = ("out of memory", "cuda error", "cublas", "no kernel image", "alloc")


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _OOM_MARKERS)


class TranscribeEngine:
    """Moteur résident, thread-safe, qui transcrit un audio.

    Args:
        config: configuration TranscrIA (backend STT, device, idle_timeout).
        backend_factory: fabrique le transcripteur (`create_transcriber` par défaut) ;
            injectable pour les tests. Le backend doit exposer
            `transcribe(path, language=...) -> list[dict]`.
    """

    def __init__(self, config: dict,
                 backend_factory: Callable[[str | None], object] | None = None) -> None:
        self.config = config or {}
        live_cfg = (self.config.get("live") or {}).get("facade") or {}
        self.idle_timeout_s: float = float(live_cfg.get("stt_idle_timeout_s", 600))
        self._backend_factory = backend_factory or self._default_backend_factory
        self._backends: dict[str, object] = {}     # un backend résident PAR moteur demandé
        self._load = SerializedLoadTracker("transcribe", logger)
        self._last_used: float = 0.0

    def _default_backend_factory(self, backend: str | None) -> object:
        # Différé : la pile STT ne se charge qu'au premier appel — le nœud boote léger.
        from transcria.stt.transcriber_factory import create_transcriber
        return create_transcriber(self.config, backend=backend)

    # ── État ──────────────────────────────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return bool(self._backends)

    def status(self) -> dict:
        status = {
            "name": "transcribe",
            "loaded": self.loaded,
            "backends": sorted(self._backends),
            "idle_timeout_s": self.idle_timeout_s,
            "last_used_epoch": round(self._last_used, 3) if self._last_used else None,
        }
        status.update(self._load.snapshot())
        return status

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def _ensure_loaded(self, backend: str | None) -> object:
        key = backend or "_default"
        existing = self._backends.get(key)
        if existing is not None:
            return existing
        logger.info("Chargement du transcripteur | backend=%s", key)
        try:
            created = self._backend_factory(backend)
        except Exception as exc:  # noqa: BLE001
            if _is_oom(exc):
                logger.warning("Chargement STT refusé — VRAM saturée : %s", exc)
                raise GpuBusyError("VRAM saturée au chargement du moteur STT") from exc
            logger.exception("Échec chargement du transcripteur")
            raise UnprocessableError(f"chargement_stt_impossible: {exc}") from exc
        self._backends[key] = created
        return created

    def unload(self) -> bool:
        with self._load.acquire("unload"):
            if not self._backends:
                return False
            logger.info("Déchargement des transcripteurs (libération VRAM)")
            backends, self._backends = self._backends, {}
            for backend in backends.values():
                offload = getattr(backend, "offload", None)
                if callable(offload):
                    try:
                        offload()
                    except Exception:  # noqa: BLE001 — best effort
                        pass
            return True

    def maybe_unload_if_idle(self) -> bool:
        if not self._backends or not self._last_used or self.idle_timeout_s <= 0:
            return False
        if (time.monotonic() - self._last_used) >= self.idle_timeout_s:
            logger.info("Idle-timeout atteint (%.0fs) — déchargement STT", self.idle_timeout_s)
            return self.unload()
        return False

    # ── Transcription ─────────────────────────────────────────────────────────

    def transcribe(self, audio_path: Path, *, language: str = "fr",
                   backend: str | None = None) -> list[dict]:
        """Transcrit un audio. SÉRIALISÉ par verrou (un calcul GPU à la fois).

        Retourne les segments du pipeline (dicts `start`/`end`/`text`). Lève `GpuBusyError`
        si la VRAM est saturée — la frontale peut alors réessayer ou basculer de nœud.
        """
        started = time.monotonic()
        with self._load.acquire("transcribe"):
            engine = self._ensure_loaded(backend)
            try:
                segments = engine.transcribe(audio_path, language=language)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    logger.warning("Transcription refusée — VRAM saturée : %s", exc)
                    raise GpuBusyError("VRAM saturée pendant la transcription") from exc
                logger.exception("Erreur inattendue pendant la transcription")
                raise UnprocessableError(f"transcription_impossible: {exc}") from exc
            self._last_used = time.monotonic()

        items = [seg for seg in (segments or []) if isinstance(seg, dict)]
        logger.info("Transcription produite | segments=%d elapsed=%.2fs",
                    len(items), time.monotonic() - started)
        return items

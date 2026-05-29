"""Diariseur distant — appelle `inference_service` au lieu de charger pyannote.

Implémente `BaseDiarizer` : le pipeline ne voit aucune différence avec le
diariseur local. Réutilise les helpers de persistance hérités (cache, clips,
embeddings). En cas d'indisponibilité du service et si `fallback_local` est
activé, bascule sur le `DiarizerService` local.
"""
from __future__ import annotations

import logging
from pathlib import Path

from transcria.inference.client import (
    InferenceClient,
    InferenceUnavailable,
    build_client_from_config,
)
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)


class RemoteDiarizer(BaseDiarizer):
    """Diarisation déléguée au service d'inférence distant.

    Args:
        config: configuration complète (section `inference`).
        device: device cible du fallback local uniquement.
        client: client injecté (tests). Sinon construit depuis la config.
    """

    def __init__(self, config: dict, device: str = "cuda:0", client: InferenceClient | None = None):
        super().__init__(config, device)
        inf = config.get("inference", {}) or {}
        self.fallback_local: bool = bool(
            (inf.get("diarization", {}) or {}).get("fallback_local", inf.get("fallback_local", True))
        )
        self._client = client or build_client_from_config(config)
        self._model_id = config.get("models", {}).get(
            "pyannote_model", "pyannote/speaker-diarization-community-1"
        )

    @property
    def model_name(self) -> str:
        # Distinct du local : le cache checkpoint ne doit pas être confondu entre modes.
        return f"remote:{self._model_id}"

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── Diarisation ─────────────────────────────────────────────────────────--

    def diarize(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        cached = self._load_cached_result(fs, audio_path)
        if cached is not None:
            logger.info("Diarization (remote): checkpoint réutilisé (%d locuteurs)", len(cached.get("speakers", [])))
            return cached

        if self._client is None:
            return self._fallback_or_fail(job, audio_path, fs, reason="aucun client distant configuré")

        try:
            logger.info("Diarization (remote): appel du service pour %s", audio_path.name)
            result = self._client.diarize(audio_path)
        except InferenceUnavailable as exc:
            logger.warning("Diarization (remote): service indisponible — %s", exc)
            return self._fallback_or_fail(job, audio_path, fs, reason=str(exc))

        self._persist(fs, audio_path, result)
        logger.info(
            "Diarization (remote): %d locuteurs, %d segments",
            len(result.get("speakers") or []), len(result.get("turns") or []),
        )
        return result

    # ── Persistance & fallback ──────────────────────────────────────────────--

    def _persist(self, fs: JobFilesystem, audio_path: Path, result: dict) -> None:
        """Écrit les artefacts job à partir du résultat canonique distant."""
        fs.save_json("speakers/speaker_turns.json", result)
        if not result.get("available"):
            return
        fs.save_json(
            "speakers/speaker_stats.json",
            {"stats": result.get("stats", {}), "speakers": result.get("speakers", [])},
        )
        self._save_cache_metadata(fs, audio_path, result)
        turns = result.get("turns", [])
        speakers = result.get("speakers", [])
        # Clips : découpage audio local (ffmpeg/torchaudio), sans modèle.
        try:
            self._extract_clips(audio_path, turns, speakers, fs)
        except Exception as exc:  # noqa: BLE001 — best effort, ne bloque pas
            logger.warning("Diarization (remote): extraction des clips échouée (ignorée): %s", exc)
        # Embeddings locuteurs : peut nécessiter un modèle local → best effort.
        try:
            self._cache_speaker_embeddings(audio_path, turns, speakers, fs)
        except Exception as exc:  # noqa: BLE001
            logger.info("Diarization (remote): embeddings locuteurs non cachés (mode distant): %s", exc)

    def _fallback_or_fail(self, job: Job, audio_path: Path, fs: JobFilesystem, *, reason: str) -> dict:
        if self.fallback_local:
            logger.warning("Diarization (remote): bascule sur le diariseur local (%s)", reason)
            from transcria.stt.diarization import DiarizerService
            return DiarizerService(self.config, device=self.device).diarize(job, audio_path)
        logger.error("Diarization (remote): échec sans fallback (%s)", reason)
        result = {"available": False, "turns": [], "speakers": [], "error": f"service_indisponible: {reason}"}
        fs.save_json("speakers/speaker_turns.json", result)
        return result

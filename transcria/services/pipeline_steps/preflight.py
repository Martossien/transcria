"""Étape PRÉ-DIAGNOSTIC AUDIO (vague B2, lot 1).

Corps extrait de ``PipelineService._run_audio_preflight`` + les lectures du
signal (artefact préflight, RMS) consommées par la normalisation.
"""
import time
from pathlib import Path

from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs


def run(svc, job: Job, audio_path: str, sl) -> dict:
    """Calcule et sauvegarde les signaux acoustiques pré-STT non bloquants."""
    from transcria.audio.preflight import AudioPreflightAnalyzer

    analyzer = AudioPreflightAnalyzer(svc.config)
    if not analyzer.enabled:
        sl.debug("[pipeline] Pré-diagnostic audio désactivé", step="audio_preflight")
        return {}

    t0 = time.monotonic()
    sl.info("[pipeline] Pré-diagnostic audio en cours", step="audio_preflight")
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_preflight",
        message="Analyse technique du signal audio",
        percent=5,
        force=True,
    )
    preflight = analyzer.analyze(Path(audio_path))
    if not preflight:
        sl.warning("[pipeline] Pré-diagnostic audio indisponible", step="audio_preflight")
        return {}

    try:
        job_fs(svc.config, job.id).save_json("metadata/audio_preflight.json", preflight)
    except Exception as exc:
        sl.warning(
            "[pipeline] Sauvegarde audio_preflight.json échouée",
            step="audio_preflight",
            error=str(exc),
        )

    sl.info(
        "[pipeline] Pré-diagnostic audio terminé",
        step="audio_preflight",
        duree=round(time.monotonic() - t0, 1),
        rms=preflight.get("rms"),
        peak=preflight.get("peak"),
        snr_db=preflight.get("estimated_snr_db"),
        bandwidth_95_hz=preflight.get("bandwidth_95_hz"),
        risk_level=preflight.get("risk_level"),
        flags=preflight.get("flags"),
    )
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_preflight",
        message="Analyse technique audio terminée",
        percent=12,
        force=True,
    )
    return preflight


def load_audio_preflight(config: dict, job: Job) -> dict:
    try:
        return job_fs(config, job.id).load_json("metadata/audio_preflight.json") or {}
    except Exception:
        return {}


def rms_from_preflight(audio_preflight: dict | None) -> float | None:
    if not audio_preflight:
        return None
    try:
        return float(audio_preflight.get("rms"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def compute_rms(audio_path: str) -> float | None:
    """Calcule le RMS du fichier audio. Retourne None en cas d'erreur."""
    try:
        import numpy as np
        import soundfile as sf
        data, _ = sf.read(audio_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        return float(np.sqrt(np.mean(data ** 2)))
    except Exception:
        return None

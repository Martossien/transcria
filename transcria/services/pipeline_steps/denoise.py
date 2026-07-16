"""Étape DÉBRUITAGE AUDIO (vague B2, lot 1).

Corps extrait de ``PipelineService._run_audio_denoise``.
"""
from pathlib import Path

from transcria.audio.denoise import AudioDenoiseService
from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs


def run(svc, job: Job, audio_path: str, mode: str, audio_preflight: dict, sl) -> str:
    """Applique un débruitage expérimental sans changer la durée audio."""

    service = AudioDenoiseService(svc.config)
    should, reasons, filters = service.should_denoise(mode, audio_preflight)
    if not should:
        sl.debug("[pipeline] Débruitage audio non appliqué", step="audio_denoise",
                 reasons=reasons)
        return audio_path

    output_path = Path(audio_path).parent / "denoised.wav"
    sl.info("[pipeline] Débruitage audio requis", step="audio_denoise",
            reasons=reasons, filters=filters)
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_denoise",
        message="Débruitage audio en cours",
        percent=30,
        force=True,
    )
    result_path = service.apply(Path(audio_path), output_path, filters)

    if result_path == Path(audio_path):
        sl.warning("[pipeline] Débruitage audio ignoré, audio original conservé",
                   step="audio_denoise")
        return audio_path

    try:
        job_fs(svc.config, job.id).save_json("metadata/audio_denoise.json", {
            "input_path": str(audio_path),
            "output_path": str(result_path),
            "mode": mode,
            "reasons": reasons,
            "filters": filters,
            "preserve_timeline": True,
            "experimental": True,
        })
    except Exception as exc:
        sl.warning("[pipeline] Sauvegarde audio_denoise.json échouée",
                   step="audio_denoise", error=str(exc))

    sl.info("[pipeline] Audio débruité",
            step="audio_denoise", output=result_path.name)
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_denoise",
        message="Débruitage audio terminé",
        percent=32,
        force=True,
    )
    return str(result_path)

"""Étape FILTRAGE DE SCÈNE AUDIO (vague B2, lot 1).

Corps extrait de ``PipelineService._run_audio_scene_filter``.
"""
from pathlib import Path

from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs


def run(svc, job: Job, audio_path: str, mode: str, audio_scene: dict, sl) -> str:
    """Met en silence certaines zones de scène sans changer la durée audio."""
    from transcria.audio.scene_filter import AudioSceneFilterService

    service = AudioSceneFilterService(svc.config)
    should, reasons, intervals = service.should_filter(mode, audio_scene or None)
    if not should:
        sl.debug("[pipeline] Filtrage scène non appliqué", step="audio_scene_filter",
                 reasons=reasons)
        return audio_path

    output_path = Path(audio_path).parent / "scene_filtered.wav"
    sl.info("[pipeline] Filtrage scène audio requis", step="audio_scene_filter",
            reasons=reasons, intervals=len(intervals))
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_scene_filter",
        message="Filtrage des zones non vocales",
        percent=29,
        force=True,
    )
    result_path = service.apply(Path(audio_path), output_path, intervals)

    if result_path == Path(audio_path):
        sl.warning("[pipeline] Filtrage scène audio ignoré, audio original conservé",
                   step="audio_scene_filter")
        return audio_path

    try:
        job_fs(svc.config, job.id).save_json("metadata/audio_scene_filter.json", {
            "input_path": str(audio_path),
            "output_path": str(result_path),
            "mode": mode,
            "reasons": reasons,
            "intervals": intervals,
            "preserve_timeline": True,
        })
    except Exception as exc:
        sl.warning("[pipeline] Sauvegarde audio_scene_filter.json échouée",
                   step="audio_scene_filter", error=str(exc))

    sl.info("[pipeline] Audio filtré par analyse de scène",
            step="audio_scene_filter", output=result_path.name)
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_scene_filter",
        message="Filtrage audio terminé",
        percent=31,
        force=True,
    )
    return str(result_path)

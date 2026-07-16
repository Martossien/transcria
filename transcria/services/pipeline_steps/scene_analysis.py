"""Étape ANALYSE DE SCÈNE AUDIO (vague B2, lot 1).

Corps extraits de ``PipelineService._run_audio_scene_analysis`` et
``_refresh_audio_quality_with_scene`` (son consommateur immédiat).
"""
import time
from pathlib import Path

from transcria.audio.scene_analyzer import AudioSceneAnalyzer
from transcria.jobs.models import Job
from transcria.quality.audio_quality import AudioQualityEvaluator
from transcria.services.pipeline_steps import job_fs


def run(svc, job: Job, audio_path: str, sl) -> dict:
    """Lance l'analyse de scène audio en subprocess isolé (pré-transcription).

    Retourne un dict de signaux (has_music, has_noise, speech_ratio,
    ratios non vocaux, gender, segments horodatés) ou ``{}`` si désactivée,
    indisponible ou en échec.
    """

    analyzer = AudioSceneAnalyzer(svc.config)
    if not analyzer.enabled:
        sl.debug("[pipeline] Analyse de scène désactivée", step="audio_scene")
        return {}

    if not analyzer.available:
        sl.warning("[pipeline] Analyse de scène non disponible (librosa manquant ?)",
                   step="audio_scene")
        return {}

    t0 = time.monotonic()
    sl.info("[pipeline] Analyse de scène en cours", step="audio_scene")
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_scene",
        message="Analyse acoustique de la scène",
        percent=15,
        force=True,
    )

    try:
        scene = analyzer.analyze(Path(audio_path))
    except Exception as exc:
        sl.warning("[pipeline] Analyse de scène échouée", step="audio_scene",
                   error=str(exc))
        return {}

    if scene:
        try:
            job_fs(svc.config, job.id).save_json("metadata/audio_scene.json", scene)
        except Exception as exc:
            sl.warning("[pipeline] Sauvegarde audio_scene.json échouée",
                       step="audio_scene", error=str(exc))

    sl.info("[pipeline] Analyse de scène terminée", step="audio_scene",
            duree=round(time.monotonic() - t0, 1),
            has_music=scene.get("has_music"),
            has_noise=scene.get("has_noise"),
            speech_ratio=scene.get("speech_ratio"),
            music_ratio=scene.get("music_ratio"),
            noise_ratio=scene.get("noise_ratio"),
            no_energy_ratio=scene.get("no_energy_ratio"),
            problem_segments=len(scene.get("problem_segments") or []))
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_scene",
        message="Analyse acoustique terminée",
        percent=22,
        force=True,
    )
    return scene


def refresh_audio_quality_with_scene(svc, job: Job, audio_scene: dict, sl) -> None:
    """Réévalue la décision qualité avec les signaux de scène disponibles."""
    if not audio_scene:
        return

    try:
        fs = job_fs(svc.config, job.id)
        summary = fs.load_json("summary/summary.json") or {}
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        evaluation = AudioQualityEvaluator(svc.config).evaluate(
            audio_analysis,
            summary,
            audio_scene=audio_scene,
            preflight=preflight,
        )
        fs.save_json("metadata/audio_quality_decision.json", evaluation)
        sl.info(
            "[pipeline] Décision qualité enrichie par l'analyse de scène",
            step="audio_quality",
            quality_level=evaluation.get("level"),
            score=evaluation.get("score"),
            reasons=evaluation.get("reasons"),
            scene_findings=evaluation.get("scene_findings"),
        )
    except Exception as exc:
        sl.warning(
            "[pipeline] Enrichissement qualité par analyse de scène échoué",
            step="audio_quality",
            error=str(exc),
        )

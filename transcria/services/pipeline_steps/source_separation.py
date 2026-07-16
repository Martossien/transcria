"""Étape SÉPARATION DE SOURCES (vague B2, lot 1).

Corps extrait de ``PipelineService._run_source_separation``.
"""
from pathlib import Path

from transcria.audio.source_separation import SourceSeparationDecider, SourceSeparationService
from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs


def run(svc, job: Job, audio_path: str, audio_scene: dict, sl) -> str:
    """Décide si Demucs doit être appliqué et exécute la séparation si besoin.

    Retourne le chemin audio à utiliser pour la transcription : soit le chemin
    d'origine (séparation refusée ou échouée), soit le chemin de la piste vocale.
    """

    audio_analysis: dict = {}
    audio_quality: dict = {}
    try:
        fs = job_fs(svc.config, job.id)
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        audio_quality = fs.load_json("metadata/audio_quality_decision.json") or {}
    except Exception as exc:
        sl.debug("[pipeline] Fichiers qualité indisponibles : %s", exc,
                 step="source_sep")

    force = bool(
        svc.config.get("workflow", {})
        .get("source_separation", {})
        .get("force", False)
    )
    enabled = bool(
        svc.config.get("workflow", {})
        .get("source_separation", {})
        .get("enabled", False)
    )
    if not enabled and not force:
        sl.debug("[pipeline] Séparation désactivée", step="source_sep")
        return audio_path

    if force:
        sl.info(
            "[pipeline] Séparation forcée (workflow.source_separation.force=true)",
            step="source_sep",
        )
        should, reasons = True, ["forced"]
    else:
        decider = SourceSeparationDecider(svc.config)
        should, reasons = decider.should_separate(
            audio_analysis,
            audio_quality,
            audio_scene=audio_scene or None,
        )

    if not should:
        sl.debug("[pipeline] Séparation non requise", step="source_sep",
                 reasons=reasons)
        return audio_path

    sl.info("[pipeline] Séparation de sources requise", step="source_sep",
            reasons=reasons)
    svc.progress.update(
        job.id,
        step="processing",
        phase="source_separation",
        message="Séparation vocale en cours",
        percent=24,
        force=True,
    )

    output_path = Path(audio_path).parent / "vocals.wav"
    service = SourceSeparationService(svc.config)
    result_path = service.separate(Path(audio_path), output_path)

    if result_path != Path(audio_path):
        sl.info("[pipeline] Audio modifié après séparation vocale",
                step="source_sep", vocals=result_path.name)
        svc.progress.update(
            job.id,
            step="processing",
            phase="source_separation",
            message="Séparation vocale terminée",
            percent=28,
            force=True,
        )
    else:
        sl.warning("[pipeline] Séparation n'a pas produit de résultat, "
                   "audio original conservé", step="source_sep")

    return str(result_path)

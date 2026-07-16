"""Étape NORMALISATION AUDIO (vague B2, lot 1).

Corps extraits de ``PipelineService._run_audio_normalization`` et
``_save_audio_normalization_metadata``. Les lectures du signal préflight
(artefact, RMS) viennent du module ``preflight``.
"""
import logging
from pathlib import Path

from transcria.audio.normalization import AudioNormalizationService
from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs
from transcria.services.pipeline_steps import preflight as preflight_step

logger = logging.getLogger(__name__)


def run(svc, job: Job, audio_path: str, mode: str, sl, audio_preflight: dict | None = None) -> str:
    """Applique une normalisation légère sans changer la durée audio."""

    service = AudioNormalizationService(svc.config)
    should, reasons, filters = service.should_normalize(mode)

    if not should:
        weak_should, weak_reasons, weak_filters = service.weak_voice_filters(
            audio_preflight or preflight_step.load_audio_preflight(svc.config, job)
        )
        if weak_should:
            sl.warning(
                "[pipeline] Audio faible — profil voix faible forcé",
                step="audio_normalization",
                reasons=weak_reasons,
                filters=weak_filters,
            )
            svc.progress.update(
                job.id,
                step="processing",
                phase="audio_normalization",
                message="Normalisation voix faible en cours",
                percent=31,
                force=True,
            )
            output_path = Path(audio_path).parent / "normalized.wav"
            result_path = service.apply(Path(audio_path), output_path, weak_filters)
            if result_path != Path(audio_path):
                save_metadata(
                    svc.config,
                    job,
                    audio_path,
                    result_path,
                    mode,
                    weak_reasons,
                    weak_filters,
                    forced=True,
                )
                sl.info("[pipeline] Audio normalisé (forcé — voix faible)",
                        step="audio_normalization", output=Path(result_path).name)
                svc.progress.update(
                    job.id,
                    step="processing",
                    phase="audio_normalization",
                    message="Normalisation audio terminée",
                    percent=33,
                    force=True,
                )
                return str(result_path)

        # Audio trop silencieux (chuchotement, micro lointain) : forcer loudnorm
        rms = preflight_step.rms_from_preflight(audio_preflight) or preflight_step.compute_rms(audio_path)
        rms_threshold = float(
            svc.config.get("workflow", {})
            .get("audio_normalization", {})
            .get("auto_loudnorm_rms_threshold", 0.02)
        )
        if rms is not None and rms < rms_threshold:
            sl.warning(
                "[pipeline] Audio très silencieux — loudnorm forcé",
                step="audio_normalization",
                rms=round(rms, 5),
                threshold=rms_threshold,
            )
            forced_filters = ["loudnorm=I=-23:TP=-2:LRA=11"]
            svc.progress.update(
                job.id,
                step="processing",
                phase="audio_normalization",
                message="Normalisation audio en cours",
                percent=31,
                force=True,
            )
            output_path = Path(audio_path).parent / "normalized.wav"
            result_path = service.apply(Path(audio_path), output_path, forced_filters)
            if result_path != Path(audio_path):
                reasons = ["audio_trop_silencieux_auto_loudnorm", f"rms={rms:.5f}"]
                filters = forced_filters
                save_metadata(
                    svc.config, job, audio_path, result_path, mode, reasons, filters, forced=True
                )
                sl.info("[pipeline] Audio normalisé (forcé — silence)",
                        step="audio_normalization", output=Path(result_path).name)
                svc.progress.update(
                    job.id,
                    step="processing",
                    phase="audio_normalization",
                    message="Normalisation audio terminée",
                    percent=33,
                    force=True,
                )
                return str(result_path)
        sl.debug("[pipeline] Normalisation audio non appliquée", step="audio_normalization",
                 reasons=reasons)
        return audio_path

    output_path = Path(audio_path).parent / "normalized.wav"
    sl.info("[pipeline] Normalisation audio requise", step="audio_normalization",
            reasons=reasons, filters=filters)
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_normalization",
        message="Normalisation audio en cours",
        percent=31,
        force=True,
    )
    result_path = service.apply(Path(audio_path), output_path, filters)

    if result_path == Path(audio_path):
        sl.warning("[pipeline] Normalisation audio ignorée, audio original conservé",
                   step="audio_normalization")
        return audio_path

    save_metadata(svc.config, job, audio_path, result_path, mode, reasons, filters)

    sl.info("[pipeline] Audio normalisé",
            step="audio_normalization", output=result_path.name)
    svc.progress.update(
        job.id,
        step="processing",
        phase="audio_normalization",
        message="Normalisation audio terminée",
        percent=33,
        force=True,
    )
    return str(result_path)


def save_metadata(
    config: dict,
    job: Job,
    input_path: str,
    result_path,
    mode: str,
    reasons: list[str],
    filters: list[str],
    forced: bool = False,
) -> None:
    try:
        payload = {
            "input_path": str(input_path),
            "output_path": str(result_path),
            "mode": mode,
            "reasons": reasons,
            "filters": filters,
            "preserve_timeline": True,
        }
        if forced:
            payload["forced"] = True
        job_fs(config, job.id).save_json("metadata/audio_normalization.json", payload)
    except Exception as exc:
        logger.warning("[pipeline] Sauvegarde audio_normalization.json échouée: %s", exc)

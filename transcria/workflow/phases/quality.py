"""Phase CONTRÔLE QUALITÉ (vague B1, lot 2).

Corps extraits de ``WorkflowRunner`` : rapport qualité (complet ou léger selon
le profil) précédé de l'enrichissement du corpus STT (proxy taux d'édition,
possible seulement ici — le SRT corrigé est définitif après relecture finale).
"""
import logging

from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.quality.light_report import run_light_quality
from transcria.quality.quality_report import QualityReporter
from transcria.stt.corpus import enrich_corpus_with_quality, parse_srt_blocks, summarize_corpus
from transcria.workflow.profiles import profile_for_job
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)


def run(runner, job: Job, config: dict) -> dict:
    runner.store.update_state(job.id, JobState.QUALITY_CHECKING)
    runner.progress.update(
        job.id,
        step="quality",
        phase="quality_checks",
        message=progress_msg(resolve_output_language(job), "quality"),
        percent=90,
        force=True,
    )
    runner._enrich_stt_corpus_quality(job, config)
    try:
        profile = profile_for_job(job)
        if profile is not None and profile.run_quality == "light":
            # Profil léger : contrôle minimal (invariants SRT), pas le rapport complet.

            result = run_light_quality(job, config)
        else:
            # Profil complet OU job legacy (profil absent) → rapport complet (inchangé).

            result = QualityReporter(config).run_all_checks(job)
        runner.store.update_state(job.id, JobState.QUALITY_CHECKED)
        runner.progress.update(
            job.id,
            step="quality",
            phase="quality_checks",
            message=progress_msg(resolve_output_language(job), "quality_done"),
            percent=92,
            force=True,
        )
        return result
    except Exception as exc:
        logger.exception("Échec contrôle qualité")
        runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {"error": str(exc)}


def enrich_stt_corpus_quality(runner, job: Job, config: dict) -> None:
    """Remplit `quality_measure` du corpus STT (proxy taux d'édition brut↔corrigé).

    Exécuté en début de qualité, donc **après** correction et relecture finale :
    le SRT corrigé est définitif. Best-effort : aucune erreur n'affecte la qualité.
    Sans SRT corrigé (correction désactivée), ne fait rien.
    """
    if not config.get("workflow", {}).get("stt_corpus", {}).get("enabled", True):
        return
    try:
        fs = runner._get_fs(config, job.id)
        corpus = fs.load_json("metadata/stt_corpus.json")
        raw_segments = fs.load_json("metadata/transcription_segments.json")
        corrected = fs.load_text("metadata/transcription_corrigee.srt")
        if not corpus or not raw_segments or not corrected:
            return
        filled = enrich_corpus_with_quality(corpus, raw_segments, parse_srt_blocks(corrected))
        if not filled:
            return
        fs.save_json("metadata/stt_corpus.json", corpus)
        summary = summarize_corpus(corpus)
        try:
            runner.store.update_extra_data(job.id, lambda extra: {**extra, "stt_corpus_summary": summary})
        except Exception as exc:
            logger.warning("Mise à jour stt_corpus_summary (qualité) ignorée: %s", exc)
        logger.info(
            "Corpus STT enrichi du proxy qualité (job=%s): %d/%d segments, taux d'édition moyen=%s",
            job.id, filled, len(corpus), summary.get("quality_measure_mean"),
        )
    except Exception as exc:
        logger.warning("Enrichissement qualité du corpus STT ignoré (job=%s): %s", job.id, exc)

"""Phase TRANSCRIPTION finale (vague B1, lot 2).

Corps extrait de ``WorkflowRunner.run_transcription``. La réservation GPU passe
par les coutures du runner (``_reserve_gpu_phase``/``_release_gpu_phase``) —
les tests les substituent à l'instance.
"""
import logging
from pathlib import Path

from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.stt.transcriber_factory import get_backend_vram_mb
from transcria.stt.transcription import Transcriber
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)


def resolve_phase_backend(job: Job, config: dict) -> tuple[str | None, str]:
    """(backend imposé par le profil ou None, backend effectif de la phase).

    §4.1 : un profil à backend imposé (ex. srt_moss) prime sur `models.stt_backend` ;
    None = comportement historique (config-driven), TOUS les profils antérieurs."""
    # Différé : cycle phases ↔ séquence (la séquence importe le registre des phases).
    from transcria.services.pipeline_sequence import resolve_profile

    profile_backend = resolve_profile(job, job.processing_mode or "").stt_backend
    return profile_backend, profile_backend or config.get("models", {}).get("stt_backend", "cohere")


def check_single_pass_envelope(job: Job, config: dict, profile_backend: str | None) -> str | None:
    """Enveloppe du single-pass MOSS (§4.1) : message d'erreur, ou None si OK.

    Mur MESURÉ (réunion réelle, 2026-07-18) : la génération unique tronque
    silencieusement au-delà de ~17 min (couverture 1053 s sur un audio de 1200 s,
    coupée au milieu d'un mot, AUCUNE erreur émise). On refuse AVANT toute dépense
    GPU au-delà de `moss.single_pass_max_s` (défaut 600). Ne s'applique QU'AU
    backend imposé par un profil (srt_moss) — `models.stt_backend: moss` global
    garde son comportement historique."""
    if profile_backend != "moss":
        return None
    from transcria.jobs.filesystem import JobFilesystem

    fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
    analysis = fs.load_json("metadata/audio_analysis.json") or {}
    duration = float(analysis.get("duration_seconds") or analysis.get("duration") or 0)
    max_s = int(config.get("moss", {}).get("single_pass_max_s", 600))
    if duration and duration > max_s:
        return (
            f"Profil single-pass (MOSS) limité aux réunions de {max_s // 60} min : "
            f"audio de {int(duration) // 60} min — au-delà, le modèle tronque la fin "
            f"sans erreur (mur mesuré). Choisir un autre profil, ou monter "
            f"moss.single_pass_max_s en connaissance de cause."
        )
    return None


def run(runner, job: Job, audio_path: str, config: dict) -> dict:
    runner.store.update_state(job.id, JobState.TRANSCRIBING)
    runner.progress.update(
        job.id,
        step="processing",
        phase="transcription",
        message=progress_msg(resolve_output_language(job), "transcribe"),
        percent=35,
        force=True,
    )

    # Différé : la factory STT tire la chaîne config+catalogues (~0,6 s).

    profile_backend, backend = resolve_phase_backend(job, config)
    envelope_error = check_single_pass_envelope(job, config, profile_backend)
    if envelope_error:
        logger.warning("[transcription] %s", envelope_error)
        runner.store.update_state(job.id, JobState.FAILED, envelope_error)
        return {"error": envelope_error}
    required_vram_mb = get_backend_vram_mb(backend, config)
    reservation, managed_by_allocator = runner._reserve_gpu_phase(
        job,
        required_vram_mb,
        "stt",
    )
    if reservation is None and runner._reclaim_vram_from_idle_arbitrage_llm(logger):
        # VRAM insuffisante mais libérable : on a stoppé notre LLM d'arbitrage inactive,
        # on retente la réservation une fois.
        reservation, managed_by_allocator = runner._reserve_gpu_phase(job, required_vram_mb, "stt")
    if reservation is None:
        # VRAM transitoire : mise en attente + alerte admin (pas FAILED).
        msg = f"VRAM insuffisante pour la transcription ({required_vram_mb} Mo requis)"
        logger.warning("[transcription] %s", msg)
        return {
            "vram_wait": True,
            "required_mb": int(required_vram_mb),
            "phase": "stt",
            "reason": msg,
            "error": msg,
        }
    gpu = reservation.gpu_index

    try:
        # Différé : le transcripteur charge la chaîne STT (torch) — rien à faire au boot.

        transcriber = Transcriber(config, gpu_index=gpu, backend=profile_backend)
        result = transcriber.transcribe(job, Path(audio_path))
        runner.progress.update(
            job.id,
            step="processing",
            phase="transcription",
            message=progress_msg(resolve_output_language(job), "transcribe_done"),
            percent=55,
            force=True,
        )
        return result
    except Exception as exc:
        logger.exception("Échec transcription")
        runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {"error": str(exc)}
    finally:
        runner._release_gpu_phase(job, "stt", managed_by_allocator)

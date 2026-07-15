"""Phase TRANSCRIPTION finale (vague B1, lot 2).

Corps extrait de ``WorkflowRunner.run_transcription``. La réservation GPU passe
par les coutures du runner (``_reserve_gpu_phase``/``_release_gpu_phase``) —
les tests les substituent à l'instance.
"""
import logging
from pathlib import Path

from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)


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
    from transcria.stt.transcriber_factory import get_backend_vram_mb

    backend = config.get("models", {}).get("stt_backend", "cohere")
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
        from transcria.stt.transcription import Transcriber

        transcriber = Transcriber(config, gpu_index=gpu)
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

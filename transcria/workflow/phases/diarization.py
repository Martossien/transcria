"""Phase DIARISATION — détection de locuteurs pyannote et diarisation finale (vague B1, lot 2).

Corps extraits de ``WorkflowRunner``. La session GPU et les sous-appels passent
par les coutures du runner (``_gpu_session``, ``_detect_speakers``,
``_pyannote_progress_callback``, ``_inject_speaker_genders``) — substituées par
les tests à l'instance ou à la classe.
"""
import logging
from pathlib import Path

from transcria.gpu.gpu_session import GPUSessionError
from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)


def pyannote_progress_callback(runner, job: Job, step: str):
    def callback(pyannote_step: str, pyannote_percent: float | None) -> None:
        message = f"Diarisation pyannote : {pyannote_step}"
        percent = None
        if pyannote_percent is not None:
            base = 50.0 if step == "summary" else 60.0
            span = 20.0 if step == "summary" else 10.0
            percent = base + (span * pyannote_percent / 100.0)
        runner.progress.update(
            job.id,
            step=step,
            phase="pyannote",
            message=message,
            percent=percent,
        )

    return callback


def run_speaker_detection(
    runner, job: Job, audio_path: str, config: dict, update_state: bool = True
) -> dict:
    """Détecte les locuteurs via pyannote.

    `update_state=True` (étape wizard autonome) publie les états globaux
    `SPEAKER_DETECTION_RUNNING`/`DONE`/`FAILED`. `update_state=False` (sous-phase
    de `run_summary`) ne touche pas à l'état du job : le résumé reste `SUMMARY_RUNNING`
    jusqu'à `SUMMARY_DONE`, et la diarisation y est best-effort (échec → résumé
    poursuit sans écraser l'état). Le résultat est toujours retourné via le dict.
    """
    if update_state:
        runner.store.update_state(job.id, JobState.SPEAKER_DETECTION_RUNNING)
    try:
        # Différés : la chaîne pyannote (torch) n'a rien à faire au boot du workflow.
        from transcria.stt.diarizer_factory import apply_speaker_hint
        from transcria.stt.speaker_detection import SpeakerDetector

        config = apply_speaker_hint(config, job.get_extra_data().get("speaker_hint"))
        detector = SpeakerDetector(config)
        progress_callback = runner._pyannote_progress_callback(
            job, "summary" if not update_state else "speakers"
        )
        if runner._cuda_available():
            with runner._gpu_session(
                job,
                "pyannote",
                runner.vram.pyannote_vram_mb,
                "speaker_detection",
            ) as gpu:
                device = f"cuda:{gpu.gpu_index}"
                logger.info(
                    "[speaker_detection] GPU sélectionné: %s (%d Mo réservés)",
                    device, runner.vram.pyannote_vram_mb,
                )
                result = runner._detect_speakers(
                    detector, job, Path(audio_path), device=device, progress_callback=progress_callback
                )
        else:
            logger.info("[speaker_detection] CUDA indisponible — pyannote sur CPU")
            device = "cpu"
            result = runner._detect_speakers(
                detector, job, Path(audio_path), device=device, progress_callback=progress_callback
            )
        if update_state:
            runner.store.update_state(job.id, JobState.SPEAKER_DETECTION_DONE)
        return result
    except GPUSessionError as exc:
        # VRAM transitoire : on n'échoue pas, on remonte `vram_wait` (mise en attente
        # + alerte admin par l'appelant). vram_mb pyannote = runner.vram.pyannote_vram_mb.
        logger.error("[speaker_detection] VRAM insuffisante: %s", exc)
        return {
            "vram_wait": True,
            "required_mb": int(runner.vram.pyannote_vram_mb),
            "phase": "speaker_detection",
            "reason": str(exc),
            "error": str(exc),
            "speakers": [],
        }
    except Exception as exc:
        logger.exception("Échec détection locuteurs")
        if update_state:
            runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {"error": str(exc), "speakers": []}


def detect_speakers(detector, job: Job, audio_path, *, device: str, progress_callback):
    try:
        return detector.detect(job, audio_path, device=device, progress_callback=progress_callback)
    except TypeError as exc:
        if "progress_callback" not in str(exc):
            raise
        return detector.detect(job, audio_path, device=device)


def run_diarization(runner, job: Job, audio_path: str, config: dict) -> dict:
    runner.store.update_state(job.id, JobState.DIARIZING)
    runner.progress.update(
        job.id,
        step="processing",
        phase="diarization",
        message=progress_msg(resolve_output_language(job), "diar"),
        percent=60,
        force=True,
    )
    try:
        # Différés : la factory de diarisation tire la chaîne pyannote/torch.
        from transcria.stt.diarizer_factory import apply_speaker_hint, create_diarizer, get_diarizer_vram_mb

        config = apply_speaker_hint(config, job.get_extra_data().get("speaker_hint"))
        diar_backend = config.get("models", {}).get("diarization_backend", "pyannote")
        diar_vram_mb = get_diarizer_vram_mb(diar_backend, config)

        # Diarisation servie à distance (nœud de ressources, backend `remote`) :
        # aucune VRAM locale à réserver. On saute le GPUSession (sinon réservation
        # fantôme de `diarization` Mo localement — et pire, le reclaim pourrait
        # stopper la LLM à tort pour une phase qui tourne à distance).
        runs_remote = runner._phase_runs_remotely("diarization")

        def _attempt_cuda() -> dict:
            with runner._gpu_session(
                job,
                diar_backend,
                diar_vram_mb,
                "diarization",
            ) as gpu:
                device = f"cuda:{gpu.gpu_index}"
                logger.info(
                    "[diarization] backend=%s, GPU sélectionné: %s (%d Mo réservés)",
                    diar_backend, device, diar_vram_mb,
                )
                diarizer = create_diarizer(
                    config,
                    device=device,
                    progress_callback=runner._pyannote_progress_callback(job, "processing"),
                )
                res = diarizer.diarize(job, Path(audio_path))
                diarizer.offload()
                return res

        if runs_remote:
            logger.info("[diarization] backend distant — aucune réservation VRAM locale")
            diarizer = create_diarizer(
                config,
                device=None,
                progress_callback=runner._pyannote_progress_callback(job, "processing"),
            )
            try:
                result = diarizer.diarize(job, Path(audio_path))
            finally:
                diarizer.offload()
        elif runner._cuda_available():
            try:
                result = _attempt_cuda()
            except GPUSessionError:
                # VRAM bloquée par notre LLM d'arbitrage inactive : on la stoppe et on
                # retente une fois avant de basculer en attente VRAM.
                if runner._reclaim_vram_from_idle_arbitrage_llm(logger):
                    result = _attempt_cuda()
                else:
                    raise
        else:
            logger.info("[diarization] CUDA indisponible — %s sur CPU", diar_backend)
            diarizer = create_diarizer(
                config,
                device="cpu",
                progress_callback=runner._pyannote_progress_callback(job, "processing"),
            )
            try:
                result = diarizer.diarize(job, Path(audio_path))
            finally:
                diarizer.offload()

        # Attribution genre par locuteur — audio_scene.json disponible à ce stade
        # (PipelineService le produit avant d'appeler run_diarization)
        fs = runner._get_fs(config, job.id)
        audio_scene = fs.load_json("metadata/audio_scene.json") or {}
        runner._inject_speaker_genders(fs, audio_scene)
        runner.progress.update(
            job.id,
            step="processing",
            phase="diarization",
            message=progress_msg(resolve_output_language(job), "diar_done"),
            percent=70,
            force=True,
        )

        return result
    except GPUSessionError as exc:
        # VRAM transitoire : mise en attente + alerte admin (pas FAILED).
        logger.error("[diarization] VRAM insuffisante: %s", exc)
        return {
            "vram_wait": True,
            "required_mb": int(diar_vram_mb),
            "phase": "diarization",
            "reason": str(exc),
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception("Échec diarisation")
        runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {"error": str(exc)}

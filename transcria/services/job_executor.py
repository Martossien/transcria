from __future__ import annotations

import os
import signal as _sig
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

from transcria.database import db
from transcria.jobs import artifact_store
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger, inject_correlation_id
from transcria.notifications.admin_alerts import alert_admin_vram_wait
from transcria.notifications.mailer import send_job_notification_async
from transcria.queue.scheduler import QueueScheduler
from transcria.queue.store import QueueStore
from transcria.services.pipeline_service import PipelineService
from transcria.workflow.transitions import (
    is_cancel_requested,
    mark_execution_cancelled,
    mark_execution_completed,
    mark_execution_failed,
    mark_execution_queued,
    mark_execution_started,
    mark_execution_waiting_vram,
)

# Modes de file dédiés aux ÉTAPES GPU synchrones routées vers le worker (frontal `web`
# sans GPU, ou reprise serveur après attente VRAM) : `summary` (run_summary) et `speakers`
# (run_speaker_detection). Le scheduler les draine comme des jobs normaux (admission
# VRAM-aware + re-queue), mais `_run_process` y exécute le runner d'étape au lieu du
# pipeline complet — le runner gère lui-même l'état du job, l'exécuteur ne libère que la
# file (pas de COMPLETED/FAILED de pipeline, pas d'e-mail propriétaire).
SUMMARY_MODE = "summary"
SPEAKER_MODE = "speakers"
REFINE_MODE = "refine"
STEP_MODES = (SUMMARY_MODE, SPEAKER_MODE, REFINE_MODE)


def _notify(cfg: dict, job, event: str, error: str | None = None,
            facts: list[tuple[str, str]] | None = None) -> None:
    """Envoie une notification email en tâche de fond. Ne lève jamais d'exception."""
    try:
        owner = job.owner if job else None
        to_email = owner.email if owner else ""
        display_name = (owner.display_name or owner.username) if owner else ""
        send_job_notification_async(
            cfg,
            to_email=to_email,
            display_name=display_name,
            job_title=job.title if job else "",
            job_id=job.id if job else "",
            event=event,
            error=error,
            facts=facts,
            locale=getattr(owner, "locale", None) if owner else None,
        )
    except Exception as exc:
        # Les notifications ne doivent jamais bloquer le pipeline, mais l'échec
        # (ex. owner détaché hors session dans le thread worker) doit rester traçable
        # — sinon une notification absente est un angle mort indébogable.
        get_structured_logger(__name__).warning(
            "Notification email ignorée (event=%s, job=%s): %s",
            event, getattr(job, "id", None), exc,
        )


class JobExecutorService:
    def __init__(self, app: Flask, config: dict, run_scheduler: bool = True):
        self.app = app
        self.config = config
        # run_scheduler=False (rôle 'web', C1) : on crée le scheduler pour POUVOIR
        # enfiler (submit_to_queue écrit en base), mais on ne démarre pas son thread —
        # c'est un process 'scheduler'/'all' qui draine la file.
        self.run_scheduler = run_scheduler
        max_workers = int(
            config.get("workflow", {})
            .get("execution", {})
            .get("max_concurrent_jobs", 1)
        )
        self.max_workers = max(1, max_workers)
        self.queue_enabled = bool(
            config.get("workflow", {}).get("queue", {}).get("enabled", True)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="transcria-worker",
        )
        self._lock = threading.Lock()
        self._queued_job_ids: set[str] = set()
        self._running_job_ids: set[str] = set()
        self._scheduler: QueueScheduler | None = None
        if self.queue_enabled:
            self._scheduler = QueueScheduler(app, config, self._run_process)
            if self.run_scheduler:
                self._scheduler.start()

    def submit_process(
        self,
        job_id: str,
        audio_path: str,
        mode: str,
        priority: int | None = None,
        scheduled_at=None,
        vram_profile: dict | None = None,
        processing_profile_id: str | None = None,
    ) -> dict:
        if self.queue_enabled and self._scheduler is not None:
            existing_entry = QueueStore.get_entry(job_id)
            if existing_entry is not None and existing_entry.status in {"waiting", "paused", "running"}:
                return {"accepted": False, "reason": "already_active"}
            # Backend `pg` : les entrées du worker (audio, contexte, mapping locuteurs)
            # doivent être en base AVANT l'enfilage. Idempotent (no-op si déjà poussé) ;
            # ré-alimente `input/` après une purge (ex. reprocess d'un job terminé).
            artifact_store.push_job_files(self.config, job_id, prefixes=artifact_store.INPUT_PREFIXES)
            return self._scheduler.submit_to_queue(
                job_id,
                mode,
                priority=priority,
                scheduled_at=scheduled_at,
                vram_profile=vram_profile,
                processing_profile_id=processing_profile_id,
            )

        with self._lock:
            if job_id in self._queued_job_ids or job_id in self._running_job_ids:
                return {"accepted": False, "reason": "already_active"}
            self._queued_job_ids.add(job_id)

        mark_execution_queued(job_id, mode, processing_profile_id)
        future = self._executor.submit(self._run_process, job_id, audio_path, mode)
        future.add_done_callback(lambda _: self._finalize_tracking(job_id))
        return {"accepted": True, "status": "queued", "mode": mode}

    def get_runtime_snapshot(self) -> dict:
        if self.queue_enabled and self._scheduler is not None:
            return self._scheduler.get_runtime_snapshot()
        with self._lock:
            return {
                "healthy": True,
                "max_workers": self.max_workers,
                "queued_jobs": len(self._queued_job_ids),
                "running_jobs": len(self._running_job_ids),
                "queue_enabled": False,
            }

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop(timeout_s=10)
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _run_process(self, job_id: str, audio_path: str, mode: str) -> None:
        sl = get_structured_logger(__name__)
        inject_correlation_id()
        sl.set_context(job_id=job_id, step="background")
        job = None
        try:
            with self.app.app_context():
                mark_execution_started(job_id)
                QueueStore.mark_running(job_id)
                with self._lock:
                    self._queued_job_ids.discard(job_id)
                    self._running_job_ids.add(job_id)

                job = JobStore.get_by_id(job_id)
                if job is None:
                    mark_execution_failed(job_id, "Job introuvable")
                    return
                if is_cancel_requested(job):
                    mark_execution_cancelled(job_id)
                    return

                # Backend `pg` (split sans filesystem partagé) : matérialise les fichiers
                # du job (audio, contexte, artefacts des dispatchs précédents) AVANT de
                # travailler — la reprise par artefact marche même sur un autre worker ou
                # après un disque vidé. Une erreur ici doit échouer le job (visible,
                # relançable) plutôt que de produire un résultat sans entrées.
                artifact_store.pull_job_files(self.config, job_id)

                is_step_mode = mode in STEP_MODES
                if is_step_mode:
                    # Étape GPU synchrone routée vers le worker (frontal sans GPU / reprise
                    # serveur) : le runner gère lui-même l'état du job (SUMMARY_DONE /
                    # SPEAKER_DETECTION_DONE / FAILED). L'exécuteur ne libère que la file.
                    from transcria.workflow.runner import WorkflowRunner

                    runner = WorkflowRunner(JobStore, self.config)  # type: ignore[arg-type]
                    if mode == SUMMARY_MODE:
                        result = runner.run_summary(job, audio_path, self.config)
                    elif mode == REFINE_MODE:
                        # Tour du chat d'affinage (job terminé) : la demande vit dans
                        # refine/request.json, le runner gère tout (best-effort).
                        result = runner.run_refine(job, self.config)
                    else:  # SPEAKER_MODE
                        result = runner.run_speaker_detection(job, audio_path, self.config, update_state=True)
                else:
                    pipeline = PipelineService(self.config)
                    result = pipeline.run_process(job, audio_path, mode, finalize_job_state=False)

                # Filet de durabilité (backend `pg`) : pousse les fichiers produits, même
                # partiels (utiles à la reprise sur un autre worker et à l'affichage
                # frontale). Les phases du pipeline ont déjà poussé à leur checkpoint ;
                # ceci couvre les étapes (`summary`/`speakers`) et les fichiers hors phase.
                # Un échec doit remonter : un résultat non durable n'est pas un résultat.
                artifact_store.push_job_files(self.config, job_id)

                if result.get("cancelled"):
                    QueueStore.dequeue(job_id, status="cancelled")
                    mark_execution_cancelled(job_id)
                    JobStore.update_state(job_id, JobState.CANCELLED)
                    if not is_step_mode:
                        self._purge_input_blobs(job_id, sl)
                elif result.get("deferred"):
                    # Mode dégradé §7.2 : ressources distantes injoignables (transitoire).
                    # On replanifie au lieu d'échouer (terminaison garantie par la fenêtre
                    # max_unavailable_s côté pré-vol). Pas d'état terminal, pas de notif.
                    retry_after = int(result.get("retry_after_s", 30))
                    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
                    QueueStore.requeue_later(job_id, scheduled_at)
                    mark_execution_queued(job_id, mode)
                    sl.info("Job différé (ressources distantes) — nouvelle tentative dans %ds",
                            retry_after, job_id=job_id, reason=result.get("reason"))
                elif result.get("vram_wait"):
                    # VRAM locale momentanément insuffisante (transitoire) : on re-queue
                    # au lieu d'échouer (reprise auto par le scheduler dès libération).
                    # Pas d'état terminal, pas de mail d'échec propriétaire. L'admin est
                    # alerté UNE seule fois (premier passage en attente).
                    required_mb = int(result.get("required_mb") or 0)
                    phase = result.get("phase") or "stt"
                    retry_after = int(result.get("retry_after_s", 30))
                    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
                    QueueStore.requeue_later(job_id, scheduled_at)
                    first_wait = mark_execution_waiting_vram(job_id, required_mb=required_mb, phase=phase)
                    sl.warning("Job en attente de VRAM — nouvelle tentative dans %ds",
                               retry_after, job_id=job_id, required_vram_mb=required_mb, phase=phase)
                    if first_wait:
                        alert_admin_vram_wait(self.config, job, required_mb=required_mb, phase=phase)
                elif result.get("error"):
                    QueueStore.dequeue(job_id, status="failed")
                    mark_execution_failed(job_id, result["error"])
                    if is_step_mode:
                        # Le runner d'étape a déjà posé l'état FAILED réel ; ce n'est pas le
                        # livrable final, on ne notifie pas le propriétaire par e-mail.
                        sl.warning("Étape GPU (worker) en échec", job_id=job_id, mode=mode, error=result["error"])
                    else:
                        JobStore.update_state(job_id, JobState.FAILED, result["error"])
                        _notify(self.config, job, "failed", result["error"])
                        self._purge_input_blobs(job_id, sl)
                elif is_step_mode:
                    # Succès d'une étape GPU (résumé / détection) : le runner a déjà posé
                    # l'état (SUMMARY_DONE / SPEAKER_DETECTION_DONE). On libère seulement la
                    # file — pas de COMPLETED ni d'e-mail (le job poursuit son parcours wizard).
                    QueueStore.dequeue(job_id, status="done")
                    mark_execution_completed(job_id)
                    sl.info("Étape GPU (worker) terminée", job_id=job_id, mode=mode)
                else:
                    QueueStore.dequeue(job_id, status="done")
                    mark_execution_completed(job_id)
                    JobStore.update_state(job_id, JobState.COMPLETED)
                    # Email « terminé » enrichi : temps réel + score qualité + points
                    # (lien vers /result). Best-effort côté collecte des faits.
                    try:
                        from transcria.notifications.job_facts import completed_facts
                        facts = completed_facts(self.config, job, result.get("processing_seconds"))
                    except Exception:  # noqa: BLE001
                        facts = None
                    _notify(self.config, job, "completed", facts=facts)
                    self._purge_input_blobs(job_id, sl)
        except Exception as exc:
            with self.app.app_context():
                QueueStore.dequeue(job_id, status="failed")
                mark_execution_failed(job_id, str(exc))
                JobStore.update_state(job_id, JobState.FAILED, str(exc))
                # `job` chargé dans le premier app_context est détaché ici (sa session
                # est fermée) : _notify accède à job.owner (lazy load) → l'email
                # d'échec serait silencieusement perdu. On recharge dans CE contexte.
                _notify(self.config, JobStore.get_by_id(job_id), "failed", str(exc))
                if mode not in STEP_MODES:
                    self._purge_input_blobs(job_id, sl)
            raise
        finally:
            self._finalize_tracking(job_id)

    def _purge_input_blobs(self, job_id: str, sl) -> None:
        """Purge les blobs `input/` (backend `pg`) à l'état terminal du pipeline complet.

        Best-effort : ne masque jamais l'issue du job. L'original reste sur le disque de
        la frontale ; un reprocess re-poussera `input/` à l'enfilage (`submit_process`).
        Pas de purge après une étape `summary`/`speakers` : l'audio resservira au worker.
        """
        try:
            artifact_store.purge_input_files(self.config, job_id)
        except Exception as exc:
            sl.warning("Purge des blobs input/ impossible (non bloquant)", job_id=job_id, error=str(exc))

    def _finalize_tracking(self, job_id: str) -> None:
        with self._lock:
            self._queued_job_ids.discard(job_id)
            self._running_job_ids.discard(job_id)


_executor_service: JobExecutorService | None = None
_executor_lock = threading.Lock()


def _kill_orphaned_opencode(job_id: str, jobs_dir: str, sl) -> None:
    """Tue les processus opencode orphelins de TranscrIA identifiés par .opencode.pid.

    Seuls les processus dont le PID est dans un .opencode.pid du répertoire du job
    sont ciblés — jamais les opencode lancés par d'autres applications sur la machine.
    """
    job_path = Path(jobs_dir) / job_id
    if not job_path.is_dir():
        return
    for pid_file in job_path.rglob(".opencode.pid"):
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            continue
        if pid <= 1:
            pid_file.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, _sig.SIGTERM)
            sl.warning(
                "Réconciliation: SIGTERM opencode orphelin PID=%d (job %s)", pid, job_id
            )
            time.sleep(2)
            try:
                os.kill(pid, 0)  # Encore vivant ?
                os.kill(pid, _sig.SIGKILL)
                sl.warning(
                    "Réconciliation: SIGKILL opencode orphelin PID=%d (job %s)", pid, job_id
                )
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass  # Déjà terminé
        except PermissionError:
            sl.warning("Réconciliation: permission refusée pour tuer PID=%d (job %s)", pid, job_id)
        finally:
            pid_file.unlink(missing_ok=True)


def _reconcile_interrupted_jobs(app: Flask, config: dict) -> None:
    """Au démarrage, récupère les jobs bloqués en 'running' suite à un redémarrage.

    Pour chaque job interrompu :
    - transcription_corrigee.srt présent → LLM a fini → COMPLETED
    - transcription.srt présent → transcription OK, correction interrompue → FAILED (relançable)
    - aucun fichier → FAILED
    """
    sl = get_structured_logger(__name__)
    jobs_dir = config.get("storage", {}).get("jobs_dir", "./jobs")

    try:
        with app.app_context():
            all_jobs = list(
                db.session.execute(db.select(Job)).scalars().all()
            )
            recovered, failed_count = 0, 0
            for job in all_jobs:
                exec_status = job.get_extra_data().get("execution", {}).get("status")
                if exec_status == "queued" and QueueStore.get_entry(job.id) is None:
                    mode = job.get_extra_data().get("execution", {}).get("mode", "fast")
                    QueueStore.enqueue(job.id, mode=mode)
                    sl.info("Réconciliation: job queued réinséré dans job_queue", job_id=job.id)
                    continue
                if exec_status != "running":
                    continue

                fs = JobFilesystem(jobs_dir, job.id)
                corrected = fs.job_dir / "metadata" / "transcription_corrigee.srt"
                transcribed = fs.job_dir / "metadata" / "transcription.srt"

                # Tuer tout opencode zombie appartenant à ce job (et uniquement lui)
                _kill_orphaned_opencode(job.id, jobs_dir, sl)

                if corrected.is_file() and corrected.stat().st_size > 0:
                    mark_execution_completed(job.id)
                    JobStore.update_state(job.id, JobState.COMPLETED)
                    sl.info(
                        "Réconciliation: job récupéré → COMPLETED",
                        job_id=job.id,
                        reason="transcription_corrigee.srt présent",
                    )
                    recovered += 1
                else:
                    reason = (
                        "transcription OK, correction interrompue"
                        if transcribed.is_file()
                        else "aucun fichier produit"
                    )
                    mark_execution_failed(job.id, "Interrompu par redémarrage du service")
                    JobStore.update_state(
                        job.id, JobState.FAILED, "Interrompu par redémarrage du service"
                    )
                    sl.warning(
                        "Réconciliation: job → FAILED (relançable)",
                        job_id=job.id,
                        reason=reason,
                    )
                    failed_count += 1

            if recovered or failed_count:
                sl.info(
                    "Réconciliation terminée",
                    recovered=recovered,
                    failed=failed_count,
                )
    except Exception as exc:
        sl.warning("Réconciliation: erreur ignorée", error=str(exc))


def init_job_executor(app: Flask, config: dict, run_scheduler: bool = True) -> JobExecutorService:
    global _executor_service
    with _executor_lock:
        _executor_service = JobExecutorService(app, config, run_scheduler=run_scheduler)
        # Réconciliation des jobs interrompus : tâche d'orchestration → uniquement si
        # ce process draine la file (rôle scheduler/all), pas dans le tier web.
        if run_scheduler:
            _reconcile_interrupted_jobs(app, config)
        return _executor_service


def get_job_executor() -> JobExecutorService | None:
    return _executor_service


def shutdown_job_executor() -> None:
    global _executor_service
    with _executor_lock:
        if _executor_service is not None:
            _executor_service.stop()
            _executor_service = None

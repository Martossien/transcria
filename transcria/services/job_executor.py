from __future__ import annotations

import os
import signal as _sig
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger, inject_correlation_id
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
)


def _notify(cfg: dict, job, event: str, error: str | None = None) -> None:
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
        )
    except Exception:
        pass  # Les notifications ne doivent jamais bloquer le pipeline


class JobExecutorService:
    def __init__(self, app: Flask, config: dict):
        self.app = app
        self.config = config
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
            self._scheduler.start()

    def submit_process(
        self,
        job_id: str,
        audio_path: str,
        mode: str,
        priority: int | None = None,
        scheduled_at=None,
        vram_profile: dict | None = None,
    ) -> dict:
        if self.queue_enabled and self._scheduler is not None:
            existing_entry = QueueStore.get_entry(job_id)
            if existing_entry is not None and existing_entry.status in {"waiting", "paused", "running"}:
                return {"accepted": False, "reason": "already_active"}
            return self._scheduler.submit_to_queue(
                job_id,
                mode,
                priority=priority,
                scheduled_at=scheduled_at,
                vram_profile=vram_profile,
            )

        with self._lock:
            if job_id in self._queued_job_ids or job_id in self._running_job_ids:
                return {"accepted": False, "reason": "already_active"}
            self._queued_job_ids.add(job_id)

        mark_execution_queued(job_id, mode)
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

                pipeline = PipelineService(self.config)
                result = pipeline.run_process(job, audio_path, mode, finalize_job_state=False)
                if result.get("cancelled"):
                    QueueStore.dequeue(job_id, status="cancelled")
                    mark_execution_cancelled(job_id)
                    JobStore.update_state(job_id, JobState.CANCELLED)
                elif result.get("error"):
                    QueueStore.dequeue(job_id, status="failed")
                    mark_execution_failed(job_id, result["error"])
                    JobStore.update_state(job_id, JobState.FAILED, result["error"])
                    _notify(self.config, job, "failed", result["error"])
                else:
                    QueueStore.dequeue(job_id, status="done")
                    mark_execution_completed(job_id)
                    JobStore.update_state(job_id, JobState.COMPLETED)
                    _notify(self.config, job, "completed")
        except Exception as exc:
            with self.app.app_context():
                QueueStore.dequeue(job_id, status="failed")
                mark_execution_failed(job_id, str(exc))
                JobStore.update_state(job_id, JobState.FAILED, str(exc))
                _notify(self.config, job, "failed", str(exc))
            raise
        finally:
            self._finalize_tracking(job_id)

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


def init_job_executor(app: Flask, config: dict) -> JobExecutorService:
    global _executor_service
    with _executor_lock:
        _executor_service = JobExecutorService(app, config)
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

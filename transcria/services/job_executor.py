from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask

from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger, inject_correlation_id
from transcria.services.pipeline_service import PipelineService
from transcria.workflow.transitions import (
    is_cancel_requested,
    mark_execution_cancelled,
    mark_execution_completed,
    mark_execution_failed,
    mark_execution_queued,
    mark_execution_started,
)


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
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="transcria-worker",
        )
        self._lock = threading.Lock()
        self._queued_job_ids: set[str] = set()
        self._running_job_ids: set[str] = set()

    def submit_process(self, job_id: str, audio_path: str, mode: str) -> dict:
        with self._lock:
            if job_id in self._queued_job_ids or job_id in self._running_job_ids:
                return {"accepted": False, "reason": "already_active"}
            self._queued_job_ids.add(job_id)

        mark_execution_queued(job_id, mode)
        future = self._executor.submit(self._run_process, job_id, audio_path, mode)
        future.add_done_callback(lambda _: self._finalize_tracking(job_id))
        return {"accepted": True, "status": "queued", "mode": mode}

    def get_runtime_snapshot(self) -> dict:
        with self._lock:
            return {
                "healthy": True,
                "max_workers": self.max_workers,
                "queued_jobs": len(self._queued_job_ids),
                "running_jobs": len(self._running_job_ids),
            }

    def _run_process(self, job_id: str, audio_path: str, mode: str) -> None:
        sl = get_structured_logger(__name__)
        inject_correlation_id()
        sl.set_context(job_id=job_id, step="background")
        try:
            with self.app.app_context():
                mark_execution_started(job_id)
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
                result = pipeline.run_process(job, audio_path, mode)
                if result.get("cancelled"):
                    mark_execution_cancelled(job_id)
                elif result.get("error"):
                    mark_execution_failed(job_id, result["error"])
                else:
                    mark_execution_completed(job_id)
        except Exception as exc:
            with self.app.app_context():
                mark_execution_failed(job_id, str(exc))
            raise

    def _finalize_tracking(self, job_id: str) -> None:
        with self._lock:
            self._queued_job_ids.discard(job_id)
            self._running_job_ids.discard(job_id)


_executor_service: JobExecutorService | None = None
_executor_lock = threading.Lock()


def init_job_executor(app: Flask, config: dict) -> JobExecutorService:
    global _executor_service
    with _executor_lock:
        _executor_service = JobExecutorService(app, config)
        return _executor_service


def get_job_executor() -> JobExecutorService | None:
    return _executor_service

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.queue.allocator import GPUAllocator
from transcria.queue.calendar import SchedulingCalendar
from transcria.queue.store import QueueStore
from transcria.workflow.transitions import get_execution_status, is_cancel_requested, mark_execution_queued

ProcessFn = Callable[[str, str, str], None]


class QueueScheduler:
    """Scheduler persistant minimal : draine job_queue et lance les jobs éligibles."""

    def __init__(self, app: Flask, config: dict, process_fn: ProcessFn):
        self.app = app
        self.config = config
        self.process_fn = process_fn
        queue_cfg = config.get("workflow", {}).get("queue", {}) or {}
        execution_cfg = config.get("workflow", {}).get("execution", {}) or {}
        self.poll_interval_s = max(1, int(queue_cfg.get("poll_interval_s", 5)))
        self.aging_enabled = bool(queue_cfg.get("aging_enabled", True))
        self.aging_interval_minutes = int(queue_cfg.get("aging_interval_minutes", 30))
        self.aging_max_bonus = int(queue_cfg.get("aging_max_bonus", 49))
        self.max_workers = max(1, min(int(execution_cfg.get("max_concurrent_jobs", 1)), 8))
        self.jobs_dir = config.get("storage", {}).get("jobs_dir", "./jobs")
        self.calendar = SchedulingCalendar(config.get("workflow", {}).get("scheduling", {}) or {})
        self.allocator = GPUAllocator.get_instance(config)

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="transcria-queue-worker",
        )
        self._running: dict[str, Future] = {}
        self._running_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._total_dispatched = 0
        self._last_iteration_s = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="transcria-queue-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 30) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)
        self._executor.shutdown(wait=False, cancel_futures=False)

    def wake(self) -> None:
        self._wake_event.set()

    def submit_to_queue(
        self,
        job_id: str,
        mode: str,
        priority: int | None = None,
        scheduled_at: datetime | None = None,
        vram_profile: dict | None = None,
    ) -> dict:
        queue_cfg = self.config.get("workflow", {}).get("queue", {}) or {}
        entry = QueueStore.enqueue(
            job_id,
            priority=priority if priority is not None else queue_cfg.get("default_priority", 50),
            scheduled_at=scheduled_at,
            vram_profile=vram_profile,
            mode=mode,
        )
        mark_execution_queued(job_id, mode)
        self.wake()
        return {
            "accepted": True,
            "status": "queued",
            "mode": mode,
            "queue_id": entry.id,
            "priority": entry.base_priority,
            "position": QueueStore.get_position(job_id),
        }

    def _dispatch_loop(self) -> None:
        sl = get_structured_logger(__name__)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                with self.app.app_context():
                    dispatched = self._dispatch_iteration()
                    self._last_iteration_s = time.monotonic() - started
                    if dispatched:
                        sl.info(
                            "Dispatch queue",
                            dispatched=dispatched,
                            running_jobs=self.running_count,
                            iteration_s=round(self._last_iteration_s, 3),
                        )
            except Exception as exc:
                # En cours d'arrêt, la base peut déjà être fermée : ne pas polluer
                # les logs avec l'erreur de la dernière itération avortée.
                if not self._stop_event.is_set():
                    sl.warning("Dispatch queue ignoré après erreur", error=str(exc))
            if self._stop_event.is_set():
                break
            self._wake_event.wait(self.poll_interval_s)
            self._wake_event.clear()

    @property
    def running_count(self) -> int:
        with self._running_lock:
            return len(self._running)

    def _dispatch_iteration(self) -> int:
        if self.aging_enabled:
            QueueStore.apply_aging(
                interval_minutes=self.aging_interval_minutes,
                max_total_bonus=self.aging_max_bonus,
            )
        if self.calendar.is_queue_paused():
            return 0
        effective_max = self.calendar.get_effective_max_workers(self.max_workers)
        capacity = effective_max - self.running_count
        if capacity <= 0:
            return 0

        dispatched = 0
        for entry in QueueStore.get_next_candidates(limit=max(16, capacity)):
            if dispatched >= capacity:
                break
            with self._running_lock:
                if entry.job_id in self._running:
                    continue
            job = JobStore.get_by_id(entry.job_id)
            if job is None:
                QueueStore.dequeue(entry.job_id, status="failed")
                continue
            if job.state == "cancelled" or get_execution_status(job) == "cancelled" or is_cancel_requested(job):
                QueueStore.dequeue(entry.job_id, status="cancelled")
                continue
            audio_path = JobFilesystem(self.jobs_dir, entry.job_id).get_original_audio_path()
            if audio_path is None:
                QueueStore.dequeue(entry.job_id, status="failed")
                continue
            if not self._first_phase_resources_available(entry):
                continue
            if self._launch(entry.job_id, str(Path(audio_path)), entry.mode):
                dispatched += 1
        return dispatched

    def _first_phase_resources_available(self, entry) -> bool:
        profile = entry.get_vram_profile()
        phases = profile.get("phases") if isinstance(profile, dict) else {}
        required_mb = int((phases or {}).get("stt") or 0)
        if required_mb <= 0:
            return True
        if self.allocator.can_allocate(required_mb) is not None:
            return True
        if self.calendar.is_force_gpu_allowed():
            self.allocator.force_free_gpu(self.allocator.preferred_gpu, allow_kill=True)
            return self.allocator.can_allocate(required_mb) is not None
        return False

    def _launch(self, job_id: str, audio_path: str, mode: str) -> bool:
        # Claim atomique (Phase B / C2) : transition WAITING→RUNNING en base. Si une
        # autre instance a déjà pris l'entrée (ou si elle n'est plus waiting), on
        # abandonne proprement — pas de double-dispatch.
        if not QueueStore.claim(job_id):
            return False
        future = self._executor.submit(self.process_fn, job_id, audio_path, mode)
        with self._running_lock:
            self._running[job_id] = future
            self._total_dispatched += 1
        future.add_done_callback(lambda fut, jid=job_id: self._on_done(jid, fut))  # type: ignore[misc]
        return True

    def _on_done(self, job_id: str, future: Future) -> None:
        with self._running_lock:
            self._running.pop(job_id, None)
        self.wake()

    def get_runtime_snapshot(self) -> dict:
        counts = QueueStore.count_by_status()
        return {
            "healthy": True,
            "max_workers": self.max_workers,
            "queued_jobs": counts.get("waiting", 0),
            "running_jobs": self.running_count,
            "paused_jobs": counts.get("paused", 0),
            "queue_enabled": True,
            "total_dispatched": self._total_dispatched,
            "last_iteration_s": round(self._last_iteration_s, 3),
        }

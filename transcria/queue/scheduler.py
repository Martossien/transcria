from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask

from transcria.database import db
from transcria.inference.client import InferenceClientError, build_client_from_config
from transcria.inference.resource_status import available_remote_slots, remote_requirements, remote_vram_admits
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.queue.allocator import GPUAllocator
from transcria.queue.calendar import SchedulingCalendar
from transcria.queue.notify_listener import QueueNotifyListener
from transcria.queue.scheduler_lock import SchedulerLock
from transcria.queue.store import QueueStore
from transcria.workflow.transitions import get_execution_status, is_cancel_requested, mark_execution_queued

logger = logging.getLogger(__name__)

ProcessFn = Callable[[str, str, str], None]


@dataclass(frozen=True)
class _RemoteDispatchState:
    slots: int | None = None
    capabilities: dict | None = None


class QueueScheduler:
    """Scheduler persistant minimal : draine job_queue et lance les jobs éligibles."""

    def __init__(self, app: Flask, config: dict, process_fn: ProcessFn):
        self.app = app
        self.config = config
        self.process_fn = process_fn
        queue_cfg = config.get("workflow", {}).get("queue", {}) or {}
        execution_cfg = config.get("workflow", {}).get("execution", {}) or {}
        self.poll_interval_s = max(1, int(queue_cfg.get("poll_interval_s", 5)))
        # Réveil instantané cross-process via LISTEN/NOTIFY (B9). Désactivé par défaut :
        # le polling suffit en mono-process. Utile en rôles web/scheduler séparés (C1).
        self.use_listen_notify = bool(queue_cfg.get("use_listen_notify", False))
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
        self._singleton_lock: SchedulerLock | None = None
        self._notify_listener: QueueNotifyListener | None = None
        self._total_dispatched = 0
        self._last_iteration_s = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Garde-fou « ordonnanceur unique » (C1 / I1) : verrou consultatif PostgreSQL.
        # Si un autre process draine déjà la file, on NE démarre PAS de second thread.
        self._singleton_lock = SchedulerLock(db.engine)
        if not self._singleton_lock.try_acquire():
            self._singleton_lock = None
            logger.error(
                "Un ordonnanceur de file tourne déjà (verrou consultatif détenu) — "
                "ce process ne démarre pas le sien (invariant : scheduler unique)."
            )
            return
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="transcria-queue-scheduler",
            daemon=True,
        )
        self._thread.start()
        # Réveil instantané (B9) : optionnel, en complément du polling (filet de sûreté).
        if self.use_listen_notify:
            self._notify_listener = QueueNotifyListener.for_engine(
                db.engine, self.wake, timeout_s=self.poll_interval_s
            )
            if self._notify_listener is not None:
                self._notify_listener.start()

    @property
    def has_singleton_lock(self) -> bool:
        return self._singleton_lock is not None and self._singleton_lock.acquired

    def stop(self, timeout_s: float = 30) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._notify_listener is not None:
            self._notify_listener.stop()
            self._notify_listener = None
        if self._thread:
            self._thread.join(timeout=timeout_s)
        self._executor.shutdown(wait=False, cancel_futures=False)
        if self._singleton_lock is not None:
            self._singleton_lock.release()
            self._singleton_lock = None

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
        self.wake()  # réveil intra-process (rôle 'all')
        if self.use_listen_notify:
            # Réveil cross-process (rôle 'web' → process 'scheduler') ; best-effort.
            try:
                QueueStore.notify_queue()
            except Exception as exc:  # noqa: BLE001 — le polling reste le filet de sûreté
                logger.warning("NOTIFY file ignoré (%s) — réveil au prochain poll", exc)
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
        # Capacité lue en base (autorité cross-process, C1) plutôt que sur le dict
        # en mémoire : reste correct si plusieurs workers d'exécution coexistent.
        capacity = effective_max - QueueStore.count_running()
        if capacity <= 0:
            return 0
        remote_state = self._remote_dispatch_state()
        if remote_state.slots is not None:
            if remote_state.slots <= 0:
                logger.info("Dispatch queue différé: nœud de ressources saturé")
                return 0
            if remote_state.slots < capacity:
                logger.info("Dispatch queue borné par le nœud de ressources: capacity=%d remote_slots=%d", capacity, remote_state.slots)
                capacity = remote_state.slots

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
            if not self._resources_available(entry, remote_state.capabilities):
                continue
            if self._launch(entry.job_id, str(Path(audio_path)), entry.mode):
                dispatched += 1
        return dispatched

    def _resources_available(self, entry, remote_capabilities: dict | None = None) -> bool:
        profile = entry.get_vram_profile()
        remote_vram = remote_vram_admits(self.config, remote_capabilities, profile)
        if remote_vram is False:
            logger.info("Dispatch job différé: VRAM distante insuffisante", extra={"job_id": entry.job_id})
            return False
        required_mb = self._local_required_mb(profile)
        if required_mb <= 0:
            return True
        if self.allocator.can_allocate(required_mb) is not None:
            return True
        if self.calendar.is_force_gpu_allowed():
            self.allocator.force_free_gpu(self.allocator.preferred_gpu, allow_kill=True)
            return self.allocator.can_allocate(required_mb) is not None
        return False

    def _first_phase_resources_available(self, entry) -> bool:
        # Compatibilité tests/appels historiques : B6.3 utilise désormais
        # `_resources_available`, qui couvre aussi le peak local et la VRAM distante.
        return self._resources_available(entry)

    def _local_required_mb(self, profile: dict) -> int:
        remote_phases = self._remote_phase_names()
        phases = profile.get("phases") if isinstance(profile, dict) else {}
        ignored_phases = set(remote_phases)
        if isinstance(profile, dict) and profile.get("llm_shared"):
            ignored_phases.add("llm_arbitration")
        if isinstance(phases, dict) and phases:
            values = [
                self._positive_int(required_mb)
                for phase, required_mb in phases.items()
                if phase not in ignored_phases
            ]
            return max(values, default=0)
        if isinstance(profile, dict):
            return self._positive_int(profile.get("peak_vram_mb"))
        return 0

    def _remote_phase_names(self) -> set[str]:
        reqs = remote_requirements(self.config)
        phases: set[str] = set()
        if "stt" in reqs:
            phases.update({"stt", "summary_stt"})
        if "diarize" in reqs:
            phases.add("diarization")
        if "voice_embed" in reqs:
            phases.add("voice_embed")
        return phases

    @staticmethod
    def _positive_int(value) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    def _remote_capacity_limit(self) -> int | None:
        return self._remote_dispatch_state().slots

    def _remote_dispatch_state(self) -> _RemoteDispatchState:
        """Capacité distante exploitable pour ce tick, si elle est connue.

        Best-effort : en cas de nœud injoignable ou de payload incomplet, on garde
        le comportement existant. Le pré-vol `resource_gate` gère alors le defer/fail
        avec la fenêtre d'indisponibilité configurée.
        """
        if not remote_requirements(self.config):
            return _RemoteDispatchState()
        client = build_client_from_config(self.config)
        if client is None:
            return _RemoteDispatchState()
        try:
            capabilities = client.capabilities()
        except InferenceClientError as exc:
            logger.warning("Capacité distante indisponible au dispatch — pré-vol conservé: %s", exc)
            return _RemoteDispatchState()
        slots = available_remote_slots(self.config, capabilities)
        if slots is not None:
            logger.debug("Capacité distante dispatch: slots=%s", slots)
        return _RemoteDispatchState(slots=slots, capabilities=capabilities)

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

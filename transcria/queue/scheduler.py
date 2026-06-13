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
                # Backend `pg` (split sans filesystem partagé) : l'audio vit en base et
                # n'est peut-être pas encore matérialisé sur CE disque (worker neuf, cache
                # vidé). On le matérialise AVANT de conclure « audio introuvable ».
                audio_path = self._materialize_job_inputs(entry.job_id)
            if audio_path is None:
                QueueStore.dequeue(entry.job_id, status="failed")
                continue
            if not self._resources_available(entry, remote_state.capabilities):
                continue
            if self._launch(entry.job_id, str(Path(audio_path)), entry.mode):
                dispatched += 1
        return dispatched

    def _materialize_job_inputs(self, job_id: str):
        """Matérialise `input/` depuis la base (backend `pg`) et re-résout l'audio.

        Retourne le chemin audio, ou None (backend `fs`, blob absent, ou erreur — loguée :
        le dequeue `failed` qui suit reste visible et relançable)."""
        from transcria.jobs import artifact_store

        if not artifact_store.is_pg_backend(self.config):
            return None
        try:
            artifact_store.pull_job_files(self.config, job_id, prefixes=("input/",))
        except Exception:
            logger.exception("Matérialisation de l'audio impossible au dispatch", extra={"job_id": job_id})
            return None
        return JobFilesystem(self.jobs_dir, job_id).get_original_audio_path()

    def _resources_available(self, entry, remote_capabilities: dict | None = None) -> bool:
        profile = entry.get_vram_profile()
        remote_vram = remote_vram_admits(self.config, remote_capabilities, profile)
        if remote_vram is False:
            logger.info("Dispatch job différé: VRAM distante insuffisante", extra={"job_id": entry.job_id})
            return False
        done_phases = self._done_profile_phases(entry.job_id)
        # La LLM d'arbitrage est un besoin MULTI-GPU (total ÷ nb de cartes du placement),
        # vérifié à part avec la VÉRITÉ VIVANTE : LLM en marche → réellement partagée
        # (rien à exiger) ; éteinte → chaque GPU du placement doit pouvoir l'héberger.
        # (L'ancien drapeau stocké `llm_shared` était TOUJOURS vrai — l'admission ne
        # vérifiait jamais la LLM, même éteinte : audit du 11/06/2026.)
        if not self._llm_admissible(profile, done_phases):
            logger.info(
                "Dispatch job différé: VRAM multi-GPU insuffisante pour (re)lancer la LLM d'arbitrage",
                extra={"job_id": entry.job_id},
            )
            return False
        required_mb = self._local_required_mb(profile, done_phases)
        # `required_mb` = max mono-GPU des phases NON-LLM restantes (STT/diarisation).
        if required_mb <= 0:
            return True
        if self.allocator.can_allocate(required_mb) is not None:
            return True

        # Bloqué : une phase NON-LLM manque de VRAM. Catégorie 1 (TOUJOURS, indépendante
        # du calendrier) : si NOTRE LLM d'arbitrage inactive occupe la VRAM, on l'arrête
        # proprement (elle sera relancée à la phase de correction) puis on re-teste. Sans
        # cela, un job resterait en `waiting` indéfiniment derrière notre propre LLM chaude.
        if self._reclaim_idle_arbitrage_llm():
            if self.allocator.can_allocate(required_mb) is not None:
                return True

        # Catégorie 3 (opt-in `gpu.preemption=aggressive` ET fenêtre calendaire `force_gpu`)
        # : préempter les serveurs d'inférence TIERS (kill_patterns, process non trackés).
        if self._preemption_aggressive() and self.calendar.is_force_gpu_allowed():
            self.allocator.force_free_gpu(self.allocator.preferred_gpu, allow_kill=True)
            return self.allocator.can_allocate(required_mb) is not None
        return False

    def _preemption_aggressive(self) -> bool:
        policy = str((self.config.get("gpu", {}) or {}).get("preemption", "own-only")).strip().lower()
        return policy == "aggressive"

    def _reclaim_idle_arbitrage_llm(self) -> bool:
        """Catégorie 1 : arrête NOTRE LLM d'arbitrage inactive pour libérer la VRAM.

        Mutualise la logique avec `WorkflowRunner` via `stop_idle_arbitrage_llm`.
        Best-effort : ne bloque jamais le dispatch.
        """
        try:
            from transcria.gpu.vram_reclaim import stop_idle_arbitrage_llm

            return stop_idle_arbitrage_llm(self.allocator, self._vram_manager(), log=logger)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reclaim LLM d'arbitrage (admission) impossible: %s", exc)
            return False

    def _vram_manager(self):
        """VRAMManager paresseux (config seule, sans effet GPU à la construction)."""
        vram = getattr(self, "_vram", None)
        if vram is None:
            from transcria.gpu.vram_manager import VRAMManager

            vram = VRAMManager(self.config)
            self._vram = vram
        return vram

    def _first_phase_resources_available(self, entry) -> bool:
        # Compatibilité tests/appels historiques : B6.3 utilise désormais
        # `_resources_available`, qui couvre aussi le peak local et la VRAM distante.
        return self._resources_available(entry)

    def _done_profile_phases(self, job_id: str) -> set[str]:
        """Phases du profil VRAM déjà réalisées (pipeline reprenable) → à exclure du besoin.

        Mappe les phases de reprise (`completed_phases`) vers les clés du profil VRAM :
        un job dont il ne reste que la correction n'exige plus que la VRAM LLM, pas le STT.
        Voir docs/PIPELINE_REPRISE.md.
        """
        try:
            from transcria.workflow.resume import get_completed_phases

            job = JobStore.get_by_id(job_id)
            if job is None:
                return set()
            done = get_completed_phases(job)
        except Exception:  # noqa: BLE001
            return set()
        ignored: set[str] = set()
        if "transcription" in done:
            ignored |= {"stt", "summary_stt"}
        if "diarization" in done:
            ignored.add("diarization")
        if "correction" in done or "final_review" in done:
            ignored.add("llm_arbitration")
        return ignored

    def _llm_admissible(self, profile: dict, completed_profile_phases: set[str]) -> bool:
        """La phase LLM restante (s'il y en a une) peut-elle être servie ?

        - LLM **en marche** → partagée, rien à exiger (vérité vivante, pas le drapeau
          stocké `llm_shared` qui était inconditionnellement vrai) ;
        - LLM **éteinte** → `can_host_llm` : chaque GPU du placement script
          (`gpu.llm_gpu_indices`) doit avoir sa part (total ÷ nb cartes) de libre.
        Best-effort : une sonde en échec n'empêche jamais l'admission (la réservation
        en phase reste le garde-fou final)."""
        phases = profile.get("phases") if isinstance(profile, dict) else {}
        llm_mb = self._positive_int((phases or {}).get("llm_arbitration"))
        if llm_mb <= 0 or "llm_arbitration" in completed_profile_phases:
            return True
        if "llm_arbitration" in self._remote_phase_names():
            return True
        try:
            if self._vram_manager().is_arbitrage_llm_running():
                return True
        except Exception:  # noqa: BLE001 — sonde best-effort
            return True
        try:
            return self.allocator.can_host_llm(llm_mb)
        except Exception:  # noqa: BLE001
            return True

    def _local_required_mb(self, profile: dict, completed_profile_phases: set[str] | None = None) -> int:
        remote_phases = self._remote_phase_names()
        phases = profile.get("phases") if isinstance(profile, dict) else {}
        ignored_phases = set(remote_phases)
        ignored_phases |= (completed_profile_phases or set())  # phases déjà faites (reprise)
        # La LLM est un besoin MULTI-GPU : jamais dans le max mono-GPU (cf. _llm_admissible).
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
        # Surface une exception non gérée du thread de job (sinon avalée par le Future) :
        # `_run_process` gère et re-lève déjà ses erreurs, mais une défaillance dans son
        # propre `except`/`finally` disparaîtrait sans aucune trace.
        try:
            exc = future.exception()
        except Exception:  # noqa: BLE001 — future annulé / état inattendu : ne pas masquer la suite
            exc = None
        if exc is not None:
            logger.error("Thread de job terminé sur exception non gérée (job=%s)", job_id, exc_info=exc)
        # Filet de sécurité VRAM : réclame toute réservation d'accounting résiduelle de CE
        # job (idempotent ; ne touche PAS le verrou LLM, déjà géré par les `finally` du
        # pipeline). Pour un process unique à longue vie gérant une ressource rare, une
        # réservation fuitée ampute la VRAM jusqu'à la famine d'admission. Si ce filet
        # récupère réellement quelque chose, c'est qu'une phase n'a pas libéré → WARNING.
        try:
            reclaimed = self.allocator.release_reservations(job_id)
            if reclaimed:
                logger.warning(
                    "Filet de sécurité : %d Mo de réservation GPU récupérés (job=%s) — "
                    "une phase n'avait pas libéré, à investiguer", reclaimed, job_id,
                )
        except Exception:  # noqa: BLE001 — le nettoyage ne doit jamais masquer l'issue du job
            logger.exception("Filet de sécurité GPU échoué (job=%s)", job_id)
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

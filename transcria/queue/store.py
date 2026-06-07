from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import case, func, or_, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupMembership, Role
from transcria.database import db
from transcria.jobs.models import Job
from transcria.queue.models import JobQueueEntry
from transcria.queue.notify_listener import QUEUE_NOTIFY_CHANNEL

logger = logging.getLogger(__name__)

QUEUE_WAITING = "waiting"
QUEUE_PAUSED = "paused"
QUEUE_RUNNING = "running"
QUEUE_DONE = "done"
QUEUE_CANCELLED = "cancelled"
QUEUE_FAILED = "failed"


class QueueStore:
    @staticmethod
    def enqueue(
        job_id: str,
        priority: int = 50,
        scheduled_at: datetime | None = None,
        vram_profile: dict | None = None,
        mode: str = "fast",
    ) -> JobQueueEntry:
        priority = QueueStore._normalize_priority(priority)
        existing = QueueStore.get_entry(job_id)
        if existing is not None:
            return QueueStore._refresh_entry(existing, priority, scheduled_at, vram_profile, mode)

        entry = JobQueueEntry(
            job_id=job_id,
            base_priority=priority,
            aging_bonus=0,
            position=QueueStore._next_position(priority),
            status=QUEUE_WAITING,
            submitted_at=datetime.now(timezone.utc),
            scheduled_at=scheduled_at,
            mode=mode,
        )
        entry.set_vram_profile(vram_profile)
        db.session.add(entry)
        try:
            db.session.commit()
        except IntegrityError:
            # Course double-submit : un INSERT concurrent pour le même job_id a gagné
            # (contrainte unique `job_queue.job_id`). On récupère l'entrée gagnante et on
            # la réutilise → enqueue idempotent, pas de 500. La correction (un seul job
            # en file, pas de double-run) était déjà assurée par la contrainte ; ici on
            # rend la course gracieuse côté API.
            db.session.rollback()
            existing = QueueStore.get_entry(job_id)
            if existing is None:
                raise  # IntegrityError sans lien avec l'unicité job_id → ne pas masquer
            logger.warning(
                "enqueue: insertion concurrente détectée pour job_id=%s — réutilisation de l'entrée existante", job_id
            )
            return QueueStore._refresh_entry(existing, priority, scheduled_at, vram_profile, mode)
        return entry

    @staticmethod
    def _refresh_entry(
        existing: JobQueueEntry,
        priority: int,
        scheduled_at: datetime | None,
        vram_profile: dict | None,
        mode: str,
    ) -> JobQueueEntry:
        """Réutilise une entrée existante (re-enqueue idempotent). Une entrée terminée
        (done/cancelled/failed) repasse WAITING ; sinon on met seulement à jour les
        métadonnées (priorité, planification, mode, profil VRAM)."""
        if existing.status in {QUEUE_DONE, QUEUE_CANCELLED, QUEUE_FAILED}:
            existing.status = QUEUE_WAITING
            existing.started_at = None
            existing.gpu_index = None
            existing.current_phase = None
        existing.base_priority = priority
        existing.scheduled_at = scheduled_at
        existing.mode = mode
        existing.set_vram_profile(vram_profile)
        db.session.commit()
        return existing

    @staticmethod
    def notify_queue() -> None:
        """Émet ``NOTIFY transcria_queue`` pour réveiller l'ordonnanceur (B9, PostgreSQL).

        No-op hors PostgreSQL. Petite transaction dédiée : la notification est délivrée
        au commit. Best-effort — un échec ne doit pas faire échouer l'enqueue (le polling
        de l'ordonnanceur reste le filet de sûreté)."""
        if db.engine.dialect.name != "postgresql":
            return
        db.session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": QUEUE_NOTIFY_CHANNEL})
        db.session.commit()

    @staticmethod
    def dequeue(job_id: str, status: str = QUEUE_DONE) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        entry.status = status
        entry.current_phase = None
        entry.gpu_index = None
        entry.aging_bonus = 0
        db.session.commit()
        return True

    @staticmethod
    def requeue_later(job_id: str, scheduled_at: datetime) -> bool:
        """Replanifie un job en cours pour une nouvelle tentative différée.

        Remet l'entrée en WAITING avec un `scheduled_at` futur : le scheduler ignore
        les entrées dont `scheduled_at > now`, donc le job patiente puis est re-pris.
        Utilisé par le mode dégradé §7.2 (ressources distantes injoignables → on diffère
        au lieu d'échouer). La terminaison reste garantie côté pré-vol via
        `inference.resilience.max_unavailable_s`.
        """
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        entry.status = QUEUE_WAITING
        entry.started_at = None
        entry.gpu_index = None
        entry.current_phase = None
        entry.scheduled_at = scheduled_at
        db.session.commit()
        return True

    @staticmethod
    def delete_entry(job_id: str) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        db.session.delete(entry)
        db.session.commit()
        return True

    @staticmethod
    def get_entry(job_id: str) -> JobQueueEntry | None:
        return db.session.execute(
            db.select(JobQueueEntry).filter_by(job_id=job_id)
        ).scalar_one_or_none()

    @staticmethod
    def get_ordered_queue(limit: int = 100, include_running: bool = False) -> list[JobQueueEntry]:
        statuses = [QUEUE_WAITING, QUEUE_PAUSED]
        if include_running:
            statuses.append(QUEUE_RUNNING)
        return list(
            db.session.execute(
                db.select(JobQueueEntry)
                .filter(JobQueueEntry.status.in_(statuses))
                .order_by(
                    (JobQueueEntry.base_priority - JobQueueEntry.aging_bonus).asc(),
                    JobQueueEntry.position.asc(),
                    JobQueueEntry.submitted_at.asc(),
                )
                .limit(limit)
            ).scalars().all()
        )

    @staticmethod
    def get_visible_queue(user, limit: int = 100) -> list[JobQueueEntry]:
        query = db.select(JobQueueEntry).join(Job)
        if not user.has_role(Role.ADMIN):
            group_ids = GroupStore.user_group_ids(user.id, admin_only=True)
            if not group_ids:
                return []
            owner_ids = db.select(GroupMembership.user_id).filter(GroupMembership.group_id.in_(group_ids))
            query = query.filter(or_(Job.owner_id == user.id, Job.owner_id.in_(owner_ids)))
        return list(
            db.session.execute(
                query.filter(JobQueueEntry.status.in_([QUEUE_WAITING, QUEUE_PAUSED, QUEUE_RUNNING]))
                .order_by(
                    (JobQueueEntry.base_priority - JobQueueEntry.aging_bonus).asc(),
                    JobQueueEntry.position.asc(),
                    JobQueueEntry.submitted_at.asc(),
                )
                .limit(limit)
            ).scalars().all()
        )

    @staticmethod
    def get_position(job_id: str) -> int | None:
        ordered = QueueStore.get_ordered_queue(limit=10000)
        for index, entry in enumerate(ordered, start=1):
            if entry.job_id == job_id:
                return index
        return None

    @staticmethod
    def get_next_candidates(limit: int = 16) -> list[JobQueueEntry]:
        now = datetime.now(timezone.utc)
        return list(
            db.session.execute(
                db.select(JobQueueEntry)
                .filter(JobQueueEntry.status == QUEUE_WAITING)
                .filter(or_(JobQueueEntry.scheduled_at.is_(None), JobQueueEntry.scheduled_at <= now))
                .order_by(
                    (JobQueueEntry.base_priority - JobQueueEntry.aging_bonus).asc(),
                    JobQueueEntry.position.asc(),
                    JobQueueEntry.submitted_at.asc(),
                )
                .limit(limit)
            ).scalars().all()
        )

    @staticmethod
    def move_up(job_id: str) -> bool:
        position = QueueStore.get_position(job_id)
        if position is None or position <= 1:
            return False
        return QueueStore.move_to_position(job_id, position - 1)

    @staticmethod
    def move_down(job_id: str) -> bool:
        position = QueueStore.get_position(job_id)
        if position is None:
            return False
        return QueueStore.move_to_position(job_id, position + 1)

    @staticmethod
    def move_to_position(job_id: str, new_position: int) -> bool:
        ordered = QueueStore.get_ordered_queue(limit=10000)
        target = next((entry for entry in ordered if entry.job_id == job_id), None)
        if target is None:
            return False
        ordered = [entry for entry in ordered if entry.job_id != job_id]
        new_index = max(0, min(int(new_position) - 1, len(ordered)))
        ordered.insert(new_index, target)
        for index, entry in enumerate(ordered, start=1):
            entry.position = index
        db.session.commit()
        return True

    @staticmethod
    def set_priority(job_id: str, priority: int) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        entry.base_priority = QueueStore._normalize_priority(priority)
        entry.position = QueueStore._next_position(entry.base_priority)
        db.session.commit()
        return True

    @staticmethod
    def pause(job_id: str, paused_by_user_id: str | None = None) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None or entry.status == QUEUE_RUNNING:
            return False
        entry.status = QUEUE_PAUSED
        entry.paused_by = paused_by_user_id
        db.session.commit()
        return True

    @staticmethod
    def resume(job_id: str) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None or entry.status != QUEUE_PAUSED:
            return False
        entry.status = QUEUE_WAITING
        entry.paused_by = None
        db.session.commit()
        return True

    @staticmethod
    def claim(job_id: str) -> bool:
        """Transition **atomique** WAITING→RUNNING, sûre quel que soit le nombre de
        dispatchers (Phase B / C2).

        Renvoie ``True`` si *cet* appelant a remporté l'entrée (et l'a passée RUNNING),
        ``False`` si elle n'était plus disponible (déjà prise, terminée ou absente).
        C'est le primitif anti-double-dispatch (limite #3) : une entrée n'est lancée
        qu'une seule fois, même si deux orchestrateurs coexistent.

        - **PostgreSQL** : verrou ligne ``FOR UPDATE SKIP LOCKED`` — un dispatcher
          concurrent visant la même ligne la *saute* (pas d'attente) et repart sur une
          autre. Le ``filter(status=waiting)`` garantit qu'une entrée déjà running/terminée
          n'est jamais re-revendiquée.
        - **Autres dialectes** (SQLite dev/tests) : UPDATE conditionnel atomique
          (``WHERE status='waiting'`` + ``rowcount``).

        La transaction est volontairement minuscule (aucune E/S) : on claim, on committe,
        *puis* on lance le job hors transaction (cf. C2 — verrous tenus quelques ms).
        """
        now = datetime.now(timezone.utc)
        if db.engine.dialect.name == "postgresql":
            entry = db.session.execute(
                db.select(JobQueueEntry)
                .filter_by(job_id=job_id, status=QUEUE_WAITING)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if entry is None:
                db.session.rollback()   # libère la transaction ouverte par le SELECT
                return False
            entry.status = QUEUE_RUNNING
            entry.started_at = now
            db.session.commit()
            return True
        result = cast(
            CursorResult,
            db.session.execute(
                update(JobQueueEntry)
                .where(JobQueueEntry.job_id == job_id, JobQueueEntry.status == QUEUE_WAITING)
                .values(status=QUEUE_RUNNING, started_at=now)
            ),
        )
        db.session.commit()
        return result.rowcount == 1

    @staticmethod
    def mark_running(job_id: str, gpu_index: int | None = None, phase: str | None = None) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        entry.status = QUEUE_RUNNING
        entry.started_at = datetime.now(timezone.utc)
        entry.gpu_index = gpu_index
        entry.current_phase = phase
        db.session.commit()
        return True

    @staticmethod
    def update_phase(job_id: str, phase: str | None, gpu_index: int | None = None) -> bool:
        entry = QueueStore.get_entry(job_id)
        if entry is None:
            return False
        entry.current_phase = phase
        entry.gpu_index = gpu_index
        db.session.commit()
        return True

    @staticmethod
    def apply_aging(interval_minutes: int = 30, max_total_bonus: int = 49) -> int:
        max_bonus = max(0, int(max_total_bonus))
        if max_bonus <= 0:
            return 0
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=max(1, int(interval_minutes)))
        result = db.session.execute(
            update(JobQueueEntry)
            .where(JobQueueEntry.status == QUEUE_WAITING)
            .where(JobQueueEntry.aging_bonus < max_bonus)
            .where(func.coalesce(JobQueueEntry.last_aging_at, JobQueueEntry.submitted_at) <= cutoff)
            .values(
                aging_bonus=case(
                    (JobQueueEntry.aging_bonus + 1 > max_bonus, max_bonus),
                    else_=JobQueueEntry.aging_bonus + 1,
                ),
                last_aging_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        changed = max(0, int(cast(CursorResult, result).rowcount or 0))
        if changed:
            db.session.commit()
        return changed

    @staticmethod
    def count_running() -> int:
        """Nombre de jobs RUNNING **lu en base** (autorité cross-process, Phase B / C1).

        Remplace le comptage en mémoire (`len(self._running)`) pour le calcul de
        capacité : reste correct même si plusieurs workers d'exécution coexistent.
        """
        return int(db.session.scalar(
            db.select(func.count(JobQueueEntry.id)).filter(JobQueueEntry.status == QUEUE_RUNNING)
        ) or 0)

    @staticmethod
    def count_by_status() -> dict[str, int]:
        rows = db.session.execute(
            db.select(JobQueueEntry.status, func.count(JobQueueEntry.id)).group_by(JobQueueEntry.status)
        ).all()
        return {status: count for status, count in rows}

    @staticmethod
    def estimate_wait_time(job_id: str, average_job_duration_s: int = 1800) -> float | None:
        position = QueueStore.get_position(job_id)
        if position is None:
            return None
        return float(max(0, position - 1) * max(1, average_job_duration_s))

    @staticmethod
    def _normalize_priority(priority: int) -> int:
        try:
            value = int(priority)
        except (TypeError, ValueError):
            value = 50
        return max(1, min(100, value))

    @staticmethod
    def _next_position(priority: int) -> int:
        current = db.session.scalar(
            db.select(func.max(JobQueueEntry.position)).filter(
                JobQueueEntry.base_priority == int(priority)
            )
        )
        return int(current or 0) + 1

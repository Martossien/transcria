from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupMembership, Role
from transcria.database import db
from transcria.jobs.models import Job
from transcria.queue.models import JobQueueEntry

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
        db.session.commit()
        return entry

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
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=max(1, int(interval_minutes)))
        changed = 0
        entries = db.session.execute(
            db.select(JobQueueEntry).filter(JobQueueEntry.status == QUEUE_WAITING)
        ).scalars().all()
        for entry in entries:
            last = entry.last_aging_at or entry.submitted_at
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last and last > cutoff:
                continue
            if int(entry.aging_bonus or 0) >= max_total_bonus:
                entry.last_aging_at = now
                continue
            entry.aging_bonus = min(max_total_bonus, int(entry.aging_bonus or 0) + 1)
            entry.last_aging_at = now
            changed += 1
        if changed:
            db.session.commit()
        return changed

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


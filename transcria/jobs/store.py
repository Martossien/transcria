from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupMembership, Role
from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState


class JobStore:
    @staticmethod
    def create_job(owner_id: str, title: str = "Réunion sans titre") -> Job:
        job = Job(owner_id=owner_id, title=title, state=JobState.CREATED.value)
        db.session.add(job)
        db.session.commit()
        return job

    @staticmethod
    def get_by_id(job_id: str) -> Job | None:
        return db.session.get(Job, job_id)

    @staticmethod
    def list_for_user(user, include_all: bool = False) -> list[Job]:
        if include_all or user.has_role(Role.ADMIN):
            return list(db.session.execute(db.select(Job).order_by(Job.created_at.desc())).scalars().all())
        group_ids = GroupStore.user_group_ids(user.id)
        if group_ids:
            owner_ids = db.select(GroupMembership.user_id).filter(GroupMembership.group_id.in_(group_ids))
            return list(
                db.session.execute(
                    db.select(Job)
                    .filter(or_(Job.owner_id == user.id, Job.owner_id.in_(owner_ids)))
                    .order_by(Job.created_at.desc())
                ).scalars().all()
            )
        return list(
            db.session.execute(
                db.select(Job).filter_by(owner_id=user.id).order_by(Job.created_at.desc())
            ).scalars().all()
        )

    @staticmethod
    def update_state(job_id: str, state: JobState, error_message: str | None = None) -> Job | None:
        job = db.session.get(Job, job_id)
        if job is None:
            return None
        if state in (JobState.FAILED, JobState.CANCELLED):
            extra = job.get_extra_data()
            extra["last_non_terminal_state"] = job.state
            job.set_extra_data(extra)
        job.state = state.value
        if error_message is not None:
            job.error_message = error_message
        db.session.commit()
        return job

    @staticmethod
    def update(job_id: str, **kwargs) -> Job | None:
        job = db.session.get(Job, job_id)
        if job is None:
            return None
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        db.session.commit()
        return job

    @staticmethod
    def update_extra_data(job_id: str, updater) -> Job | None:
        job = db.session.get(Job, job_id)
        if job is None:
            return None
        extra = job.get_extra_data()
        new_extra = updater(dict(extra))
        job.set_extra_data(new_extra or {})
        db.session.commit()
        return job

    @staticmethod
    def delete_job(job_id: str) -> bool:
        job = db.session.get(Job, job_id)
        if job is None:
            return False
        db.session.delete(job)
        db.session.commit()
        return True

    @staticmethod
    def count_jobs() -> int:
        return db.session.scalar(db.select(func.count(Job.id)))

    @staticmethod
    def purge_expired_jobs(retention_days: int | str | None, jobs_dir: str) -> int:
        try:
            days = int(retention_days)
        except (TypeError, ValueError):
            return 0
        if days <= 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal_states = {
            JobState.COMPLETED.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }
        jobs = db.session.execute(db.select(Job).filter(Job.state.in_(terminal_states))).scalars().all()
        purged = 0
        for job in jobs:
            updated_at = job.updated_at
            if updated_at is None:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at >= cutoff:
                continue
            JobFilesystem(jobs_dir, job.id).cleanup()
            db.session.delete(job)
            purged += 1

        if purged:
            db.session.commit()
        return purged

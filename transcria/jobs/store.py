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
    def count_waiting_vram() -> int:
        """Nombre de jobs en attente de VRAM (statut d'exécution transitoire).

        Filtre best-effort sur le JSON `extra_data_json` (portable SQLite/PostgreSQL) :
        sert au bandeau d'alerte admin. Retourne 0 hors contexte DB.
        """
        try:
            return int(
                db.session.scalar(
                    db.select(func.count(Job.id)).filter(
                        Job.extra_data_json.like('%"status": "waiting_vram"%')
                    )
                )
                or 0
            )
        except Exception:  # noqa: BLE001
            return 0

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
        # Invariant : `error_message` ne reflète qu'un échec COURANT. Toute transition
        # hors FAILED l'efface (sinon un vieux message — ex. « VRAM insuffisante » d'une
        # tentative précédente — reste collé après une reprise réussie et trompe l'UI).
        # Le détail par exécution reste tracé dans `extra_data.execution.last_error`.
        if state == JobState.FAILED:
            if error_message is not None:
                job.error_message = error_message
        else:
            job.error_message = error_message  # défaut None → efface l'ancien message
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
        # Verrou de ligne pour le read-modify-write : `extra_data_json` est un point de
        # contention partagé entre tiers/threads — la frontale y écrit (annulation,
        # édition de contexte), le scheduler y écrit fréquemment pendant le traitement
        # (progression, marqueurs de reprise, statut d'exécution). Sans `FOR UPDATE`, deux
        # écritures concurrentes du MÊME job se perdent (lost-update) — p.ex. une demande
        # d'annulation écrasée par une mise à jour de progression. `with_for_update()`
        # sérialise (le 2ᵉ lecteur attend le 1ᵉ commit) ; `populate_existing` force la
        # relecture de la valeur committée sous le verrou (et non un instantané périmé du
        # cache d'identité). FOR UPDATE est émis en PostgreSQL et ignoré sans erreur en
        # SQLite (mono-process, sérialisé par le verrou de fichier). Motif déjà éprouvé
        # dans `QueueStore.claim()`.
        stmt = (
            db.select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        job = db.session.execute(stmt).scalar_one_or_none()
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
    def purge_expired_jobs(retention_days: int | str | None, jobs_dir: str, *, dry_run: bool = False) -> int:
        try:
            days = int(retention_days)  # type: ignore[arg-type]
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
            if dry_run:                       # C3.10 : comptage sans effet de bord
                purged += 1
                continue
            JobFilesystem(jobs_dir, job.id).cleanup()
            db.session.delete(job)
            purged += 1

        if purged and not dry_run:
            db.session.commit()
        return purged

"""Tests C3.10 — purge de rétention avec dry-run (docs/RELEASE_0.2.0.md, AUDIT_DPO.md)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from transcria.database import db
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore


def _clear_jobs():
    db.session.query(Job).delete()
    db.session.commit()


def _old_terminal_job(owner_id, jobs_dir, days_ago=400):
    job = Job(owner_id=owner_id, title="Vieux", state=JobState.COMPLETED.value)
    db.session.add(job)
    db.session.commit()
    job.updated_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.commit()
    return job


class TestPurgeDryRun:
    def test_dry_run_compte_sans_supprimer(self, app, owner_id, tmp_path):
        with app.app_context():
            _clear_jobs()
            job = _old_terminal_job(owner_id, str(tmp_path))
            job_id = job.id
            counted = JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True)
            assert counted >= 1
            # le job est TOUJOURS là après un dry-run
            assert db.session.get(Job, job_id) is not None

    def test_purge_reelle_supprime(self, app, owner_id, tmp_path):
        with app.app_context():
            _clear_jobs()
            job = _old_terminal_job(owner_id, str(tmp_path))
            job_id = job.id
            purged = JobStore.purge_expired_jobs(365, str(tmp_path))
            assert purged >= 1
            assert db.session.get(Job, job_id) is None

    def test_retention_zero_ne_purge_rien(self, app, owner_id, tmp_path):
        with app.app_context():
            _clear_jobs()
            _old_terminal_job(owner_id, str(tmp_path))
            assert JobStore.purge_expired_jobs(0, str(tmp_path)) == 0
            assert JobStore.purge_expired_jobs(None, str(tmp_path)) == 0

    def test_job_recent_non_purge(self, app, owner_id, tmp_path):
        with app.app_context():
            _clear_jobs()
            job = _old_terminal_job(owner_id, str(tmp_path), days_ago=10)
            assert JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True) == 0
            assert db.session.get(Job, job.id) is not None

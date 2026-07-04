"""Tests C3.10 — purge de rétention avec dry-run (docs/archive/RELEASE_0.2.0.md, AUDIT_DPO.md).

La base de test est partagée (session) et d'autres tests y laissent des jobs/entrées de
file : ces tests raisonnent donc en DELTA (avant/après) et sur un job PRÉCIS, sans jamais
vider les tables (un TRUNCATE casserait les contraintes de clé étrangère job_queue→jobs).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from transcria.database import db
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore


def _old_terminal_job(owner_id, days_ago=400):
    job = Job(owner_id=owner_id, title="Vieux", state=JobState.COMPLETED.value)
    db.session.add(job)
    db.session.commit()
    job.updated_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.commit()
    return job


class TestPurgeDryRun:
    def test_dry_run_compte_sans_supprimer(self, app, owner_id, tmp_path):
        with app.app_context():
            before = JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True)
            job = _old_terminal_job(owner_id)
            job_id = job.id
            after = JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True)
            assert after == before + 1          # mon job est compté
            assert db.session.get(Job, job_id) is not None   # mais toujours présent

    def test_purge_reelle_supprime(self, app, owner_id, tmp_path):
        with app.app_context():
            job = _old_terminal_job(owner_id)
            job_id = job.id
            purged = JobStore.purge_expired_jobs(365, str(tmp_path))
            assert purged >= 1
            assert db.session.get(Job, job_id) is None       # bien supprimé

    def test_retention_zero_ou_none_ne_purge_rien(self, app, owner_id, tmp_path):
        with app.app_context():
            job = _old_terminal_job(owner_id)
            assert JobStore.purge_expired_jobs(0, str(tmp_path)) == 0
            assert JobStore.purge_expired_jobs(None, str(tmp_path)) == 0
            assert db.session.get(Job, job.id) is not None

    def test_job_recent_non_purge(self, app, owner_id, tmp_path):
        with app.app_context():
            before = JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True)
            job = _old_terminal_job(owner_id, days_ago=10)   # récent
            after = JobStore.purge_expired_jobs(365, str(tmp_path), dry_run=True)
            assert after == before                # un job récent n'est PAS compté
            assert db.session.get(Job, job.id) is not None

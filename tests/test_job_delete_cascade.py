"""Suppression d'un job avec dépendances en base — régression de l'incident du 11/06/2026.

POST /jobs/<id>/delete renvoyait 500 sur un job déjà passé en file : la relation ORM
Job↔JobQueueEntry sans cascade faisait « désassocier » (UPDATE job_id=NULL) au lieu de
supprimer → violation NOT NULL. Même classe de bug sur voice_matches (violation FK).
Corrigé par cascade delete-orphan sur les deux relations.
"""
from __future__ import annotations

from transcria.database import db
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.models import JobQueueEntry
from transcria.queue.store import QueueStore
from transcria.voice.models import VoiceConsent, VoiceMatch, VoiceProfile, VoiceSubject


def test_delete_job_with_done_queue_entry(app, admin_client, owner_id):
    """Le scénario exact de l'incident : job traité (entrée de file `done`) puis supprimé."""
    with app.app_context():
        job = JobStore.create_job(owner_id, "Réunion à supprimer")
        JobStore.update_state(job.id, JobState.COMPLETED)
        QueueStore.enqueue(job.id, mode="quality")
        QueueStore.dequeue(job.id, status="done")
        job_id = job.id

    resp = admin_client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
    assert resp.status_code == 200  # plus de 500

    with app.app_context():
        assert JobStore.get_by_id(job_id) is None
        assert db.session.query(JobQueueEntry).filter_by(job_id=job_id).count() == 0


def test_delete_job_with_voice_match(app, admin_client, owner_id):
    """Un job avec suggestions de matching vocal se supprime aussi (violation FK avant)."""
    with app.app_context():
        job = JobStore.create_job(owner_id, "Réunion avec voix")
        subject = VoiceSubject(display_name="Sujet Test", created_by=owner_id)
        db.session.add(subject)
        db.session.flush()
        consent = VoiceConsent(subject_id=subject.id, form_version="v1", uploaded_by=owner_id)
        db.session.add(consent)
        db.session.flush()
        profile = VoiceProfile(
            subject_id=subject.id, consent_id=consent.id, created_by=owner_id,
            embedding_backend="test", embedding_model_id="test-model",
        )
        db.session.add(profile)
        db.session.flush()
        db.session.add(VoiceMatch(
            job_id=job.id, speaker_id="SPEAKER_00",
            subject_id=subject.id, profile_id=profile.id, score=0.9,
        ))
        db.session.commit()
        job_id, subject_id = job.id, subject.id

    resp = admin_client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        assert JobStore.get_by_id(job_id) is None
        assert db.session.query(VoiceMatch).filter_by(job_id=job_id).count() == 0
        # Le sujet et le profil (référentiel de voix) survivent à la suppression du job.
        assert db.session.get(VoiceSubject, subject_id) is not None


def test_delete_job_in_waiting_queue(app, admin_client, owner_id):
    """Suppression d'un job encore EN FILE (waiting) : l'entrée part avec le job."""
    with app.app_context():
        job = JobStore.create_job(owner_id, "Réunion en attente")
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
        QueueStore.enqueue(job.id, mode="fast")
        job_id = job.id

    resp = admin_client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        assert JobStore.get_by_id(job_id) is None
        assert db.session.query(JobQueueEntry).filter_by(job_id=job_id).count() == 0

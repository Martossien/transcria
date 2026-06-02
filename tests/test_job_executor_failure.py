"""Sur exception inattendue du pipeline, le worker marque FAILED et notifie le
propriétaire — avec un job rechargé dans le bon app_context (sinon job.owner serait
détaché et l'email d'échec silencieusement perdu).
"""
from __future__ import annotations

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QueueStore
from transcria.services.job_executor import JobExecutorService


def test_unexpected_exception_marks_failed_and_notifies_attached_job(app, owner_id, monkeypatch):
    svc = JobExecutorService(app, {"workflow": {"queue": {"enabled": False}}})
    captured: dict = {}
    try:
        with app.app_context():
            job = JobStore.create_job(owner_id, "Crash job")
            JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
            QueueStore.enqueue(job.id, mode="fast")
            job_id = job.id

        def boom(self, job, audio_path, mode, finalize_job_state=False):
            raise RuntimeError("crash pipeline")

        monkeypatch.setattr(
            "transcria.services.pipeline_service.PipelineService.run_process", boom
        )

        def fake_notify(cfg, job, event, error=None):
            # On vérifie que le job transmis est exploitable (attaché) : accéder à
            # owner ne doit pas lever, et l'identité doit être correcte.
            captured["event"] = event
            captured["job_id"] = job.id if job else None
            captured["owner_ok"] = bool(job and job.owner is not None)

        monkeypatch.setattr("transcria.services.job_executor._notify", fake_notify)

        try:
            svc._run_process(job_id, "/tmp/a.wav", "fast")
        except RuntimeError:
            pass  # le worker re-lève après avoir notifié

        assert captured == {"event": "failed", "job_id": job_id, "owner_ok": True}

        with app.app_context():
            assert JobStore.get_by_id(job_id).state == JobState.FAILED.value
    finally:
        svc._executor.shutdown(wait=False)

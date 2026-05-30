"""Le job_executor replanifie (sans échouer) un job dont le pré-vol renvoie deferred.

§7.2 : ressources distantes injoignables (transitoire) → QueueStore.requeue_later
avec scheduled_at futur, état exécution « queued », pas de FAILED.
"""
from __future__ import annotations

from datetime import datetime, timezone

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QUEUE_WAITING, QueueStore
from transcria.services.job_executor import JobExecutorService


def test_deferred_result_requeues_without_failing(app, owner_id, monkeypatch):
    # Queue désactivée → pas de thread scheduler lancé par le constructeur.
    svc = JobExecutorService(app, {"workflow": {"queue": {"enabled": False}}})
    try:
        with app.app_context():
            job = JobStore.create_job(owner_id, "Deferred job")
            JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
            QueueStore.enqueue(job.id, mode="quality")
            job_id = job.id

        # Le pipeline renvoie un verdict différé (ressources distantes injoignables).
        monkeypatch.setattr(
            "transcria.services.pipeline_service.PipelineService.run_process",
            lambda self, job, audio_path, mode, finalize_job_state=False: {
                "deferred": True, "retry_after_s": 45, "reason": "nœud injoignable"
            },
        )

        svc._run_process(job_id, "/tmp/a.wav", "quality")

        with app.app_context():
            entry = QueueStore.get_entry(job_id)
            assert entry.status == QUEUE_WAITING                 # re-queue, pas terminal
            assert entry.scheduled_at is not None
            # SQLite renvoie un datetime naïf (UTC) → normaliser avant comparaison.
            sched = entry.scheduled_at
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
            assert sched > datetime.now(timezone.utc)            # différé (backoff)
            assert entry.started_at is None and entry.gpu_index is None

            job2 = JobStore.get_by_id(job_id)
            assert job2.state != JobState.FAILED                 # surtout pas d'échec
            assert job2.get_extra_data().get("execution", {}).get("status") == "queued"
    finally:
        svc._executor.shutdown(wait=False)

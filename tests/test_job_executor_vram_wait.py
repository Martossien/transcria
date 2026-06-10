"""Le job_executor met en attente (sans échouer) un job dont le pipeline renvoie vram_wait.

VRAM locale momentanément insuffisante (transitoire) → QueueStore.requeue_later avec
scheduled_at futur, statut d'exécution « waiting_vram », pas de FAILED, pas de mail
d'échec propriétaire, et alerte admin envoyée UNE seule fois.
"""
from __future__ import annotations

from datetime import datetime, timezone

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QUEUE_WAITING, QueueStore
from transcria.services.job_executor import JobExecutorService


def _vram_wait_result(self, job, audio_path, mode, finalize_job_state=False):
    return {"vram_wait": True, "required_mb": 6000, "phase": "stt", "retry_after_s": 30}


def test_vram_wait_requeues_without_failing_and_alerts_admin_once(app, owner_id, monkeypatch):
    svc = JobExecutorService(app, {"workflow": {"queue": {"enabled": False}}})
    alerts: list[dict] = []
    owner_failure_notifs: list[str] = []

    monkeypatch.setattr(
        "transcria.services.pipeline_service.PipelineService.run_process", _vram_wait_result
    )
    # Capter l'alerte admin et la notification propriétaire (qui NE doit PAS partir).
    monkeypatch.setattr(
        "transcria.services.job_executor.alert_admin_vram_wait",
        lambda cfg, job, *, required_mb, phase: alerts.append({"required_mb": required_mb, "phase": phase}),
    )
    monkeypatch.setattr(
        "transcria.services.job_executor._notify",
        lambda cfg, job, event, error=None: owner_failure_notifs.append(event),
    )

    try:
        with app.app_context():
            job = JobStore.create_job(owner_id, "VRAM wait job")
            JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
            QueueStore.enqueue(job.id, mode="quality")
            job_id = job.id

        # 1er passage : re-queue + attente + alerte admin.
        svc._run_process(job_id, "/tmp/a.wav", "quality")

        with app.app_context():
            entry = QueueStore.get_entry(job_id)
            assert entry.status == QUEUE_WAITING
            sched = entry.scheduled_at
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
            assert sched > datetime.now(timezone.utc)

            job2 = JobStore.get_by_id(job_id)
            assert job2.state != JobState.FAILED
            execution = job2.get_extra_data().get("execution", {})
            assert execution.get("status") == "waiting_vram"
            assert execution.get("required_vram_mb") == 6000
            assert execution.get("phase") == "stt"

        assert len(alerts) == 1
        assert alerts[0] == {"required_mb": 6000, "phase": "stt"}
        assert "failed" not in owner_failure_notifs

        # 2e passage (toujours en attente) : re-queue à nouveau mais SANS re-alerter l'admin.
        svc._run_process(job_id, "/tmp/a.wav", "quality")
        assert len(alerts) == 1  # anti-spam : une seule alerte tant que le job reste en attente
    finally:
        svc._executor.shutdown(wait=False)

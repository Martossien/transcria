"""GOLDENS de la machine à états de l'exécuteur (vague B0 du plan qualité).

Figent le comportement ACTUEL dict → décision de ``JobExecutorService._run_process``
AVANT l'introduction des résultats typés (``PhaseOutcome``) : chaque forme de dict
observée dans le code, plus les combinaisons de clés qui prouvent la PRIORITÉ
(cancelled > deferred > vram_wait > error > succès). Ces tests doivent rester verts,
inchangés, après la migration — c'est leur raison d'être.

(La branche vram_wait seule est déjà couverte par test_job_executor_vram_wait.py.)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QUEUE_WAITING, QueueStore
from transcria.services.job_executor import JobExecutorService


@pytest.fixture()
def harness(app, owner_id, monkeypatch):
    """Un exécuteur + un job enfilé + capture des notifications propriétaire."""
    svc = JobExecutorService(app, {"workflow": {"queue": {"enabled": False}}})
    notifs: list[str] = []
    monkeypatch.setattr(
        "transcria.services.job_executor._notify",
        lambda cfg, job, event, error=None, facts=None: notifs.append(event),
    )
    monkeypatch.setattr(
        "transcria.services.job_executor.alert_admin_vram_wait",
        lambda *a, **k: None,
    )
    with app.app_context():
        job = JobStore.create_job(owner_id, "Golden outcome job")
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
        QueueStore.enqueue(job.id, mode="quality")
        job_id = job.id
    return svc, job_id, notifs


def _run_with_result(svc, job_id, monkeypatch, result: dict) -> None:
    monkeypatch.setattr(
        "transcria.services.pipeline_service.PipelineService.run_process",
        lambda self, job, audio_path, mode, finalize_job_state=False: result,
    )
    svc._run_process(job_id, "/tmp/a.wav", "quality")


class TestGoldenSingleKeys:
    def test_success_completes_and_notifies(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(svc, job_id, monkeypatch, {"status": "completed", "processing_seconds": 12.5})
        with app.app_context():
            assert JobStore.get_by_id(job_id).state == JobState.COMPLETED
            assert QueueStore.get_entry(job_id).status == "done"
        assert notifs == ["completed"]

    def test_error_fails_and_notifies(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(svc, job_id, monkeypatch, {"error": "boom métier", "step": "transcription"})
        with app.app_context():
            job = JobStore.get_by_id(job_id)
            assert job.state == JobState.FAILED
            assert QueueStore.get_entry(job_id).status == "failed"
        assert notifs == ["failed"]

    def test_cancelled_terminal_without_notify(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(svc, job_id, monkeypatch, {"error": "Traitement annulé", "cancelled": True})
        with app.app_context():
            assert JobStore.get_by_id(job_id).state == JobState.CANCELLED
            assert QueueStore.get_entry(job_id).status == "cancelled"
        assert notifs == []  # une annulation n'envoie NI mail d'échec NI mail de fin

    def test_deferred_requeues_future_without_terminal_state(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(
            svc, job_id, monkeypatch,
            {"deferred": True, "reason": "ressources distantes injoignables", "retry_after_s": 45},
        )
        with app.app_context():
            entry = QueueStore.get_entry(job_id)
            assert entry.status == QUEUE_WAITING
            sched = entry.scheduled_at
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
            assert sched > datetime.now(timezone.utc)
            job = JobStore.get_by_id(job_id)
            assert job.state not in (JobState.FAILED, JobState.COMPLETED, JobState.CANCELLED)
            assert job.get_extra_data().get("execution", {}).get("status") == "queued"
        assert notifs == []


class TestGoldenPriorities:
    """La PRIORITÉ entre clés est un comportement, pas un accident — on la fige."""

    def test_cancelled_wins_over_error(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(svc, job_id, monkeypatch, {"cancelled": True, "error": "peu importe"})
        with app.app_context():
            assert JobStore.get_by_id(job_id).state == JobState.CANCELLED
        assert notifs == []

    def test_deferred_wins_over_vram_wait_and_error(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(
            svc, job_id, monkeypatch,
            {"deferred": True, "vram_wait": True, "error": "x", "retry_after_s": 30},
        )
        with app.app_context():
            assert QueueStore.get_entry(job_id).status == QUEUE_WAITING
            assert JobStore.get_by_id(job_id).state != JobState.FAILED
        assert notifs == []

    def test_vram_wait_wins_over_error(self, harness, app, monkeypatch):
        svc, job_id, notifs = harness
        _run_with_result(
            svc, job_id, monkeypatch,
            {"vram_wait": True, "error": "x", "required_mb": 6000, "phase": "stt", "retry_after_s": 30},
        )
        with app.app_context():
            assert QueueStore.get_entry(job_id).status == QUEUE_WAITING
            job = JobStore.get_by_id(job_id)
            assert job.state != JobState.FAILED
            assert job.get_extra_data().get("execution", {}).get("status") == "waiting_vram"
        assert notifs == []

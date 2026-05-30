from datetime import datetime, timedelta, timezone

from transcria.database import db
from transcria.jobs.store import JobStore
from transcria.queue.models import JobQueueEntry
from transcria.queue.store import QUEUE_PAUSED, QUEUE_RUNNING, QUEUE_WAITING, QueueStore


def _clear_queue():
    db.session.query(JobQueueEntry).delete()
    db.session.commit()


def test_enqueue_persists_vram_profile_and_position(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Queue job")
        entry = QueueStore.enqueue(
            job.id,
            priority=25,
            vram_profile={"peak_vram_mb": 6000, "phases": {"stt": 6000}},
            mode="quality",
        )

        reloaded = QueueStore.get_entry(job.id)

        assert reloaded is not None
        assert reloaded.id == entry.id
        assert reloaded.base_priority == 25
        assert reloaded.mode == "quality"
        assert reloaded.get_vram_profile()["phases"]["stt"] == 6000
        assert QueueStore.get_position(job.id) == 1


def test_ordering_uses_effective_priority_then_position(app, owner_id):
    with app.app_context():
        _clear_queue()
        job_a = JobStore.create_job(owner_id, "A")
        job_b = JobStore.create_job(owner_id, "B")
        job_c = JobStore.create_job(owner_id, "C")

        QueueStore.enqueue(job_a.id, priority=50)
        QueueStore.enqueue(job_b.id, priority=10)
        QueueStore.enqueue(job_c.id, priority=50)

        ordered = QueueStore.get_ordered_queue()

        assert [entry.job_id for entry in ordered[:3]] == [job_b.id, job_a.id, job_c.id]


def test_move_to_position_reorders_waiting_queue(app, owner_id):
    with app.app_context():
        _clear_queue()
        job_a = JobStore.create_job(owner_id, "A")
        job_b = JobStore.create_job(owner_id, "B")
        job_c = JobStore.create_job(owner_id, "C")
        QueueStore.enqueue(job_a.id)
        QueueStore.enqueue(job_b.id)
        QueueStore.enqueue(job_c.id)

        assert QueueStore.move_to_position(job_c.id, 1) is True

        assert [entry.job_id for entry in QueueStore.get_ordered_queue()[:3]] == [
            job_c.id,
            job_a.id,
            job_b.id,
        ]


def test_pause_resume_and_mark_running(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Pause")
        QueueStore.enqueue(job.id)

        assert QueueStore.pause(job.id, paused_by_user_id=owner_id) is True
        assert QueueStore.get_entry(job.id).status == QUEUE_PAUSED
        assert QueueStore.resume(job.id) is True
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING
        assert QueueStore.mark_running(job.id, gpu_index=2, phase="stt") is True

        running = QueueStore.get_entry(job.id)
        assert running.status == QUEUE_RUNNING
        assert running.gpu_index == 2
        assert running.current_phase == "stt"


def test_apply_aging_updates_old_waiting_entries(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Aging")
        entry = QueueStore.enqueue(job.id, priority=50)
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        entry.submitted_at = old
        entry.last_aging_at = old
        db_entry = JobQueueEntry.query.filter_by(job_id=job.id).one()
        db_entry.submitted_at = old
        db_entry.last_aging_at = old
        db.session.commit()

        changed = QueueStore.apply_aging(interval_minutes=30, max_total_bonus=49)

        assert changed >= 1
        assert QueueStore.get_entry(job.id).aging_bonus == 1


def test_future_scheduled_job_is_not_next_candidate(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Future")
        QueueStore.enqueue(job.id, scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1))

        assert job.id not in [entry.job_id for entry in QueueStore.get_next_candidates()]


def test_requeue_later_defers_running_job(app, owner_id):
    """§7.2 : un job running est replanifié en WAITING avec scheduled_at futur,
    et exclu des candidats tant que le délai n'est pas écoulé."""
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Requeue later")
        QueueStore.enqueue(job.id, mode="quality")
        QueueStore.mark_running(job.id, gpu_index=3, phase="stt")

        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        assert QueueStore.requeue_later(job.id, future) is True

        entry = QueueStore.get_entry(job.id)
        assert entry.status == QUEUE_WAITING
        assert entry.started_at is None
        assert entry.gpu_index is None
        assert entry.current_phase is None
        assert entry.mode == "quality"          # préservé
        # Différé → absent des candidats éligibles.
        assert all(e.job_id != job.id for e in QueueStore.get_next_candidates())


def test_requeue_later_eligible_once_delay_elapsed(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Requeue elapsed")
        QueueStore.enqueue(job.id)
        QueueStore.mark_running(job.id)

        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        QueueStore.requeue_later(job.id, past)

        # Délai écoulé → de nouveau candidat.
        assert any(e.job_id == job.id for e in QueueStore.get_next_candidates())


def test_requeue_later_unknown_job_returns_false(app):
    with app.app_context():
        assert QueueStore.requeue_later("inexistant", datetime.now(timezone.utc)) is False

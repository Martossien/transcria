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


def test_apply_aging_ignores_recent_waiting_entries(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Recent aging")
        entry = QueueStore.enqueue(job.id, priority=50)
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        entry.submitted_at = recent
        entry.last_aging_at = recent
        db.session.commit()

        changed = QueueStore.apply_aging(interval_minutes=30, max_total_bonus=49)

        assert changed == 0
        assert QueueStore.get_entry(job.id).aging_bonus == 0


def test_apply_aging_caps_bonus_and_skips_non_waiting_entries(app, owner_id):
    with app.app_context():
        _clear_queue()
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        waiting = JobStore.create_job(owner_id, "Waiting cap")
        paused = JobStore.create_job(owner_id, "Paused aging")
        running = JobStore.create_job(owner_id, "Running aging")
        for job in (waiting, paused, running):
            entry = QueueStore.enqueue(job.id, priority=50)
            entry.submitted_at = old
            entry.last_aging_at = old
            entry.aging_bonus = 48
        QueueStore.pause(paused.id)
        QueueStore.claim(running.id)
        db.session.commit()

        changed = QueueStore.apply_aging(interval_minutes=30, max_total_bonus=49)

        assert changed == 1
        assert QueueStore.get_entry(waiting.id).aging_bonus == 49
        assert QueueStore.get_entry(paused.id).aging_bonus == 48
        assert QueueStore.get_entry(running.id).aging_bonus == 48


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


def test_count_running_reads_from_db(app, owner_id):
    with app.app_context():
        _clear_queue()
        assert QueueStore.count_running() == 0
        a = JobStore.create_job(owner_id, "A")
        b = JobStore.create_job(owner_id, "B")
        c = JobStore.create_job(owner_id, "C")
        for j in (a, b, c):
            QueueStore.enqueue(j.id)
        QueueStore.claim(a.id)
        QueueStore.claim(b.id)

        assert QueueStore.count_running() == 2   # c reste waiting


# ── Claim atomique (Phase B / C2) ──────────────────────────────────────────────

def test_claim_transitions_waiting_to_running(app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Claim me")
        QueueStore.enqueue(job.id)

        assert QueueStore.claim(job.id) is True

        entry = QueueStore.get_entry(job.id)
        assert entry.status == QUEUE_RUNNING
        assert entry.started_at is not None


def test_claim_is_single_winner(app, owner_id):
    """Deux claims séquentiels sur la même entrée : le 1er gagne, le 2nd échoue."""
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Once")
        QueueStore.enqueue(job.id)

        assert QueueStore.claim(job.id) is True
        assert QueueStore.claim(job.id) is False           # déjà running
        assert QueueStore.get_entry(job.id).status == QUEUE_RUNNING


def test_claim_rejects_non_waiting_and_missing(app, owner_id):
    with app.app_context():
        _clear_queue()
        assert QueueStore.claim("inexistant") is False     # absente

        job = JobStore.create_job(owner_id, "Paused")
        QueueStore.enqueue(job.id)
        QueueStore.pause(job.id)
        assert QueueStore.claim(job.id) is False            # statut != waiting
        assert QueueStore.get_entry(job.id).status == QUEUE_PAUSED


def test_claim_concurrent_same_entry_single_winner(app, owner_id):
    """N threads réels (connexions PG distinctes) revendiquent la MÊME entrée :
    exactement un `True`. Prouve l'absence de double-dispatch (limite #3)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            import pytest
            pytest.skip("Test de concurrence : PostgreSQL uniquement")
        _clear_queue()
        job = JobStore.create_job(owner_id, "Contended")
        QueueStore.enqueue(job.id)
        job_id = job.id                      # chaîne capturée (objet ORM détaché hors contexte)

    n = 8
    barrier = threading.Barrier(n)

    def _attempt() -> bool:
        barrier.wait()                       # maximise le chevauchement
        with app.app_context():
            return QueueStore.claim(job_id)

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: _attempt(), range(n)))

    assert sum(1 for r in results if r) == 1
    with app.app_context():
        assert QueueStore.get_entry(job_id).status == QUEUE_RUNNING


def test_claim_concurrent_pool_each_claimed_once(app, owner_id):
    """M entrées, N threads qui tentent toutes les entrées en parallèle :
    chaque entrée est revendiquée par exactement un thread (aucun doublon)."""
    import threading
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            import pytest
            pytest.skip("Test de concurrence : PostgreSQL uniquement")
        _clear_queue()
        job_ids = []
        for i in range(12):
            job = JobStore.create_job(owner_id, f"Pool {i}")
            QueueStore.enqueue(job.id)
            job_ids.append(job.id)

    n = 8
    barrier = threading.Barrier(n)

    def _drain() -> list[str]:
        barrier.wait()
        won = []
        for jid in job_ids:
            with app.app_context():
                if QueueStore.claim(jid):
                    won.append(jid)
        return won

    with ThreadPoolExecutor(max_workers=n) as pool:
        per_thread = list(pool.map(lambda _: _drain(), range(n)))

    winners = Counter(jid for won in per_thread for jid in won)
    # Chaque entrée gagnée exactement une fois ; toutes les entrées prises.
    assert set(winners) == set(job_ids)
    assert all(count == 1 for count in winners.values())
    with app.app_context():
        for jid in job_ids:
            assert QueueStore.get_entry(jid).status == QUEUE_RUNNING

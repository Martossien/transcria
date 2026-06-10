from datetime import datetime, timedelta, timezone

import pytest

from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.calendar import SchedulingWindowStore
from transcria.queue.models import JobQueueEntry, SchedulingWindow
from transcria.queue.scheduler import QueueScheduler
from transcria.queue.store import QUEUE_CANCELLED, QUEUE_RUNNING, QUEUE_WAITING, QueueStore
from transcria.services.job_executor import JobExecutorService


@pytest.fixture(autouse=True)
def _no_real_llm_reclaim(monkeypatch):
    """Garde-fou : neutralise la récupération VRAM (catégorie 1) dans les tests scheduler.

    Sans cela, sur une machine où la VRAIE LLM d'arbitrage tourne, l'admission d'un job
    bloqué exécuterait réellement le script d'arrêt. Par défaut « aucune LLM inactive à
    récupérer » ; les tests qui veulent l'exercer re-patchent explicitement.
    """
    monkeypatch.setattr(QueueScheduler, "_reclaim_idle_arbitrage_llm", lambda self: False)


def _clear_queue():
    db.session.query(JobQueueEntry).delete()
    db.session.query(SchedulingWindow).delete()
    db.session.commit()


def _config(tmp_path, enabled=True):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {
            "queue": {
                "enabled": enabled,
                "default_priority": 50,
                "aging_enabled": False,
                "poll_interval_s": 60,
            },
            "execution": {"max_concurrent_jobs": 1},
            "scheduling": {"enabled": False, "timezone": "Europe/Paris"},
        },
    }


def _job_with_audio(owner_id, cfg, title="Queued"):
    job = JobStore.create_job(owner_id, title)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.save_upload(b"fake", "audio.mp3")
    return job


def test_scheduler_dispatches_waiting_candidate(app, owner_id, tmp_path):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append((job_id, audio_path, mode)))
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 1
        assert launched == [(job.id, str(JobFilesystem(cfg["storage"]["jobs_dir"], job.id).get_original_audio_path()), "fast")]
        assert QueueStore.get_entry(job.id).status == QUEUE_RUNNING


def test_scheduler_skips_future_scheduled_candidate(app, owner_id, tmp_path):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(
            job.id,
            mode="fast",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_scheduler_dequeues_cancelled_candidate(app, owner_id, tmp_path):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        JobStore.update_state(job.id, JobState.CANCELLED)
        QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert QueueStore.get_entry(job.id).status == QUEUE_CANCELLED


def test_job_executor_uses_queue_when_enabled(app, owner_id, tmp_path):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path, enabled=True)
        job = _job_with_audio(owner_id, cfg)
        executor = JobExecutorService(app, cfg)
        executor._scheduler.stop()

        result = executor.submit_process(job.id, "ignored.mp3", "fast")

        assert result["accepted"] is True
        assert result["position"] == 1
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_scheduler_respects_pause_queue_window(app, owner_id, tmp_path):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        cfg["workflow"]["scheduling"]["enabled"] = True
        SchedulingWindowStore.create({
            "name": "pause",
            "days": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
            "start": "00:00",
            "end": "23:59",
            "action": "pause_queue",
            "enabled": True,
        })
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []


def test_scheduler_skips_candidate_when_first_phase_vram_unavailable(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 6000}})

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        monkeypatch.setattr(scheduler.allocator, "can_allocate", lambda required_mb: None)
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_scheduler_uses_peak_vram_profile_for_local_admission(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"peak_vram_mb": 12000})

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        seen = []

        def fake_can_allocate(required_mb):
            seen.append(required_mb)
            return None

        monkeypatch.setattr(scheduler.allocator, "can_allocate", fake_can_allocate)
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert seen == [12000]
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_admission_reclaims_idle_arbitrage_llm_then_dispatches(app, owner_id, tmp_path, monkeypatch):
    """Catégorie 1 : un job bloqué derrière NOTRE LLM d'arbitrage inactive est dispatché
    après l'arrêt de la LLM (récupération VRAM à l'admission, sans calendrier)."""
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 6000}})
        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))

        state = {"reclaimed": False, "alloc_calls": 0}

        def fake_can_allocate(required_mb):
            state["alloc_calls"] += 1
            return 0 if state["reclaimed"] else None   # libre seulement après reclaim

        def fake_reclaim(self):
            state["reclaimed"] = True
            return True

        monkeypatch.setattr(scheduler.allocator, "can_allocate", fake_can_allocate)
        monkeypatch.setattr(QueueScheduler, "_reclaim_idle_arbitrage_llm", fake_reclaim)

        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert state["reclaimed"] is True
        assert state["alloc_calls"] == 2               # 1er échec → reclaim → 2e succès
        assert dispatched == 1
        assert launched == [job.id]


def test_admission_no_force_free_when_own_only(app, owner_id, tmp_path, monkeypatch):
    """own-only (défaut) : pas de préemption tierce même dans la fenêtre calendaire."""
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)  # gpu.preemption absent → défaut own-only
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 6000}})
        scheduler = QueueScheduler(app, cfg, lambda *a: None)

        monkeypatch.setattr(scheduler.allocator, "can_allocate", lambda required_mb: None)
        monkeypatch.setattr(scheduler.calendar, "is_force_gpu_allowed", lambda *a, **k: True)
        forced = {"n": 0}
        monkeypatch.setattr(scheduler.allocator, "force_free_gpu",
                            lambda *a, **k: forced.__setitem__("n", forced["n"] + 1) or 0)

        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert forced["n"] == 0                        # own-only ne tue jamais de tiers
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_admission_force_free_when_aggressive_and_calendar(app, owner_id, tmp_path, monkeypatch):
    """aggressive + fenêtre calendaire : préemption tierce via force_free_gpu."""
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        cfg["gpu"] = {"preemption": "aggressive"}
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 6000}})
        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: None)

        forced = {"n": 0}
        state = {"freed": False}

        def fake_can_allocate(required_mb):
            return 0 if state["freed"] else None

        def fake_force_free(gpu, allow_kill=False):
            forced["n"] += 1
            state["freed"] = True
            return 26000

        monkeypatch.setattr(scheduler.allocator, "can_allocate", fake_can_allocate)
        monkeypatch.setattr(scheduler.calendar, "is_force_gpu_allowed", lambda *a, **k: True)
        monkeypatch.setattr(scheduler.allocator, "force_free_gpu", fake_force_free)

        scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)
        assert forced["n"] == 1                        # tier préempté (aggressive + fenêtre)


def test_scheduler_ignores_shared_llm_phase_for_initial_admission(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(
            job.id,
            mode="fast",
            vram_profile={
                "phases": {"stt": 6000, "llm_arbitration": 60000},
                "llm_shared": True,
            },
        )

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        seen = []

        def fake_can_allocate(required_mb):
            seen.append(required_mb)
            return 0 if required_mb == 6000 else None

        monkeypatch.setattr(scheduler.allocator, "can_allocate", fake_can_allocate)
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 1
        assert launched == [job.id]
        assert seen == [6000]


def test_scheduler_ignores_remote_phase_for_local_vram_admission(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        cfg["models"] = {"stt_backend": "cohere", "diarization_backend": "pyannote"}
        cfg["inference"] = {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://stt/v1"}}}}
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 60000, "diarization": 2000}})

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        monkeypatch.setattr(scheduler, "_remote_dispatch_state", lambda: type("S", (), {"slots": None, "capabilities": None})())
        monkeypatch.setattr(scheduler.allocator, "can_allocate", lambda required_mb: 0 if required_mb == 2000 else None)
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 1
        assert launched == [job.id]


def test_scheduler_limits_dispatch_with_remote_capacity(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        cfg["workflow"]["execution"]["max_concurrent_jobs"] = 4
        launched = []
        jobs = [_job_with_audio(owner_id, cfg, title=f"Remote cap {i}") for i in range(3)]
        for job in jobs:
            QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        monkeypatch.setattr(scheduler, "_remote_dispatch_state", lambda: type("S", (), {"slots": 1, "capabilities": None})())
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 1
        assert len(launched) == 1
        assert QueueStore.get_entry(launched[0]).status == QUEUE_RUNNING
        assert sum(1 for job in jobs if QueueStore.get_entry(job.id).status == QUEUE_WAITING) == 2


def test_scheduler_defers_when_remote_capacity_is_zero(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        monkeypatch.setattr(scheduler, "_remote_dispatch_state", lambda: type("S", (), {"slots": 0, "capabilities": None})())
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_scheduler_defers_when_remote_vram_is_insufficient(app, owner_id, tmp_path, monkeypatch):
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        cfg["gpu"] = {"min_free_vram_mb": 1000}
        cfg["models"] = {"stt_backend": "cohere"}
        cfg["inference"] = {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://stt/v1"}}}}
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast", vram_profile={"phases": {"stt": 6000}})

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))
        state = type("S", (), {"slots": 1, "capabilities": {"gpus": [{"index": 0, "free_mb": 6500, "total_mb": 24000}]}})()
        monkeypatch.setattr(scheduler, "_remote_dispatch_state", lambda: state)
        dispatched = scheduler._dispatch_iteration()
        scheduler._executor.shutdown(wait=True)

        assert dispatched == 0
        assert launched == []
        assert QueueStore.get_entry(job.id).status == QUEUE_WAITING


def test_second_scheduler_does_not_start_when_lock_held(app, owner_id, tmp_path):
    """Garde-fou « ordonnanceur unique » (C1) : le scheduler global de la fixture
    `app` détient déjà le verrou consultatif → un nouveau start() ne démarre PAS de
    thread (sinon double-dispatch)."""
    with app.app_context():
        cfg = _config(tmp_path)
        sched = QueueScheduler(app, cfg, lambda *_: None)
        sched.start()
        try:
            assert sched.has_singleton_lock is False
            assert sched._thread is None
        finally:
            sched.stop()


def test_web_role_executor_enqueues_without_starting_scheduler(app, owner_id, tmp_path):
    """Rôle 'web' (run_scheduler=False) : le scheduler est créé pour enfiler mais son
    thread n'est pas démarré ; submit enfile bien le job (un orchestrateur drainera)."""
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path, enabled=True)
        job = _job_with_audio(owner_id, cfg)
        svc = JobExecutorService(app, cfg, run_scheduler=False)
        try:
            assert svc._scheduler is not None      # créé (pour submit_to_queue)
            assert svc._scheduler._thread is None  # mais pas démarré
            result = svc.submit_process(job.id, "ignored.mp3", "fast")
            assert result["accepted"] is True
            assert QueueStore.get_entry(job.id).status == QUEUE_WAITING
        finally:
            svc.stop()


def test_launch_claims_atomically_no_double_dispatch(app, owner_id, tmp_path):
    """_launch revendique l'entrée : un 2nd appel (entrée déjà running) renvoie
    False et ne soumet rien — pas de double-dispatch (Phase B / C2)."""
    with app.app_context():
        _clear_queue()
        cfg = _config(tmp_path)
        launched = []
        job = _job_with_audio(owner_id, cfg)
        QueueStore.enqueue(job.id, mode="fast")

        scheduler = QueueScheduler(app, cfg, lambda job_id, audio_path, mode: launched.append(job_id))

        first = scheduler._launch(job.id, "audio.mp3", "fast")
        second = scheduler._launch(job.id, "audio.mp3", "fast")
        scheduler._executor.shutdown(wait=True)

        assert first is True
        assert second is False
        assert launched == [job.id]                       # soumis une seule fois
        assert QueueStore.get_entry(job.id).status == QUEUE_RUNNING

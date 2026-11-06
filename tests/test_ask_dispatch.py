"""Câblage du worker « Discuter avec mes réunions » dans la boucle de dispatch et l'exécuteur."""
from __future__ import annotations

import threading
import time

from transcria.queue.scheduler import QueueScheduler


def _cfg(tmp_path) -> dict:
    return {"storage": {"jobs_dir": str(tmp_path)}, "workflow": {"queue": {"enabled": True, "poll_interval_s": 1},
                                                                 "execution": {"max_concurrent_jobs": 1}}}


def test_ask_tick_lance_une_demande_a_la_fois(app, tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def ask_fn():
        calls.append(1)
        started.set()
        release.wait(5)
        return "done"

    scheduler = QueueScheduler(app, _cfg(tmp_path), lambda *a: None, ask_fn=ask_fn)
    try:
        with app.app_context():
            scheduler._ask_tick()
            assert started.wait(5)
            scheduler._ask_tick()           # déjà une en cours : rien de lancé
            scheduler._ask_tick()
        release.set()
        assert scheduler._ask_future is not None
        scheduler._ask_future.result(timeout=5)
        assert calls == [1]
        with app.app_context():
            scheduler._ask_tick()           # terminée : une nouvelle itération part
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls == [1, 1]
    finally:
        release.set()
        scheduler.stop(timeout_s=2)


def test_ask_tick_sans_fonction_ne_fait_rien(app, tmp_path):
    scheduler = QueueScheduler(app, _cfg(tmp_path), lambda *a: None)
    try:
        with app.app_context():
            scheduler._ask_tick()
        assert scheduler._ask_future is None
    finally:
        scheduler.stop(timeout_s=2)


def test_exception_du_worker_est_journalisee_pas_perdue(app, tmp_path, caplog):
    def boom():
        raise RuntimeError("base indisponible")

    scheduler = QueueScheduler(app, _cfg(tmp_path), lambda *a: None, ask_fn=boom)
    try:
        with app.app_context():
            scheduler._ask_tick()
        assert scheduler._ask_future is not None
        assert scheduler._ask_future.result(timeout=5) == "error"
        assert any("Discuter avec mes réunions" in r.getMessage() for r in caplog.records)
    finally:
        scheduler.stop(timeout_s=2)


def test_submit_ask_sans_file_traite_en_thread(app, tmp_path, monkeypatch):
    from transcria.services import job_executor as je

    processed = []
    monkeypatch.setattr(je.ask_worker, "process_next", lambda config: processed.append(config) or "done")
    cfg = {"storage": {"jobs_dir": str(tmp_path)}, "workflow": {"queue": {"enabled": False},
                                                                "execution": {"max_concurrent_jobs": 1}}}
    service = je.JobExecutorService(app, cfg, run_scheduler=False)
    try:
        assert service.submit_ask("req-1")["accepted"] is True
        deadline = time.monotonic() + 5
        while not processed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert processed == [cfg]
    finally:
        service.stop()


def test_submit_ask_avec_file_laisse_le_scheduler_faire(app, tmp_path, monkeypatch):
    from transcria.services import job_executor as je

    service = je.JobExecutorService(app, _cfg(tmp_path), run_scheduler=False)
    try:
        assert service.submit_ask("req-1") == {"accepted": True, "status": "queued", "request_id": "req-1"}
        assert service._scheduler is not None and service._scheduler.ask_fn is not None
    finally:
        service.stop()


def test_la_boucle_appelle_ask_tick_meme_quand_la_file_est_pleine(app, tmp_path, monkeypatch):
    """Relecture 2026-11-06 : placé en fin de `_dispatch_iteration`, l'appel n'était jamais atteint
    quand un job tournait (`return 0` sur capacité nulle) — les questions expiraient derrière."""
    calls = []
    scheduler = QueueScheduler(app, _cfg(tmp_path), lambda *a: None, ask_fn=lambda: calls.append(1) or "done")
    try:
        monkeypatch.setattr(scheduler, "_dispatch_iteration", lambda: 0)          # file pleine / pause
        scheduler._stop_event.clear()
        scheduler._wake_event.set()
        thread = threading.Thread(target=scheduler._dispatch_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls, "ask_tick non appelé quand le dispatch des jobs sort tôt"
    finally:
        scheduler.stop(timeout_s=2)
        thread.join(timeout=3)


def test_ask_tick_survit_a_une_exception_du_dispatch(app, tmp_path, monkeypatch):
    calls = []
    scheduler = QueueScheduler(app, _cfg(tmp_path), lambda *a: None, ask_fn=lambda: calls.append(1) or "done")
    try:
        def boom():
            raise RuntimeError("dispatch cassé")
        monkeypatch.setattr(scheduler, "_dispatch_iteration", boom)
        thread = threading.Thread(target=scheduler._dispatch_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls
    finally:
        scheduler.stop(timeout_s=2)
        thread.join(timeout=3)


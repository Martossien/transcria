"""Tests du réveil LISTEN/NOTIFY de l'ordonnanceur (Phase B / B9).

- Unitaires : `QueueNotifyListener` avec une **fausse connexion** (aucun réseau) —
  réveil, reconnexion après erreur, arrêt propre, no-op hors PostgreSQL.
- Intégration PostgreSQL : `QueueStore.notify_queue()` réveille un vrai listener.
- Câblage ordonnanceur : `submit_to_queue` n'émet `NOTIFY` que si activé.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine

from transcria.database import db
from transcria.jobs.store import JobStore
from transcria.queue.notify_listener import (
    QUEUE_NOTIFY_CHANNEL,
    QueueNotifyListener,
    engine_conninfo,
)
from transcria.queue.store import QueueStore

# ── Fausse connexion psycopg ────────────────────────────────────────────────--

class _FakeConn:
    """notifies() rejoue un script : chaque appel renvoie une liste de notifications
    (objet quelconque) ou lève l'exception scriptée ; épuisé → vide après une pause."""

    def __init__(self, script):
        self._script = list(script)
        self.listened: list[str] = []
        self.closed = False

    def execute(self, sql):
        self.listened.append(str(sql))

    def notifies(self, timeout=None):
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return iter(item)
        time.sleep(0.02)        # imite l'attente jusqu'au timeout, sans busy-loop
        return iter(())

    def close(self):
        self.closed = True


# ── Unitaires ─────────────────────────────────────────────────────────────────

def test_for_engine_none_hors_postgres():
    sqlite_engine = create_engine("sqlite://")
    assert QueueNotifyListener.for_engine(sqlite_engine, lambda: None) is None


def test_engine_conninfo_reecrit_le_scheme():
    engine = create_engine("postgresql+psycopg://u:pw@host:5432/dbname")
    info = engine_conninfo(engine)
    assert info.startswith("postgresql://")
    assert "+psycopg" not in info
    assert "host:5432" in info and "dbname" in info


def test_listener_reveille_sur_notification():
    fired = threading.Event()
    conn = _FakeConn(script=[[object()]])      # un appel → une notification
    listener = QueueNotifyListener(lambda: conn, fired.set, timeout_s=0.5)
    listener.start()
    try:
        assert listener.wait_ready(2.0)
        assert fired.wait(2.0)
        assert conn.listened and conn.listened[0] == f"LISTEN {QUEUE_NOTIFY_CHANNEL}"
    finally:
        listener.stop()
    assert conn.closed is True


def test_listener_reconnecte_apres_erreur():
    fired = threading.Event()
    bad = _FakeConn(script=[ConnectionError("perdue")])
    good = _FakeConn(script=[[object()]])
    conns = iter([bad, good])
    listener = QueueNotifyListener(lambda: next(conns), fired.set, timeout_s=0.5)
    listener.start()
    try:
        # bad lève → backoff ~1s → reconnexion sur good → réveil.
        assert fired.wait(4.0)
        assert bad.closed is True
    finally:
        listener.stop()


def test_listener_stop_idempotent():
    listener = QueueNotifyListener(lambda: _FakeConn(script=[]), lambda: None, timeout_s=0.5)
    listener.start()
    listener.stop()
    listener.stop()                              # second appel : no-op
    assert listener.running is False


# ── Intégration PostgreSQL ──────────────────────────────────────────────────--

def test_notify_queue_reveille_un_vrai_listener(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("LISTEN/NOTIFY : PostgreSQL uniquement")

        fired = threading.Event()
        listener = QueueNotifyListener.for_engine(db.engine, fired.set, timeout_s=1.0)
        assert listener is not None
        listener.start()
        try:
            assert listener.wait_ready(3.0)      # LISTEN établi avant le NOTIFY
            QueueStore.notify_queue()
            assert fired.wait(3.0)
        finally:
            listener.stop()


def test_notify_queue_noop_hors_postgres(app, monkeypatch):
    # Dialecte non-PG → notify_queue sort tôt sans exécuter pg_notify.
    with app.app_context():
        executed = {"n": 0}
        real_execute = db.session.execute
        monkeypatch.setattr(db.engine.dialect, "name", "sqlite")
        monkeypatch.setattr(
            db.session, "execute",
            lambda *a, **k: executed.__setitem__("n", executed["n"] + 1) or real_execute(*a, **k),
        )
        QueueStore.notify_queue()
        assert executed["n"] == 0                # aucune requête émise


# ── Câblage ordonnanceur ────────────────────────────────────────────────────--

def _scheduler(app, use_listen_notify):
    from transcria.queue.scheduler import QueueScheduler

    cfg = {
        "workflow": {"queue": {"use_listen_notify": use_listen_notify}, "execution": {}},
        "storage": {"jobs_dir": "./jobs"},
    }
    return QueueScheduler(app, cfg, lambda *a: None)


def test_submit_to_queue_emet_notify_si_active(app, owner_id, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(QueueStore, "notify_queue", lambda: calls.__setitem__("n", calls["n"] + 1))
    with app.app_context():
        job = JobStore.create_job(owner_id, "Notify on")
        _scheduler(app, use_listen_notify=True).submit_to_queue(job.id, mode="fast")
    assert calls["n"] == 1


def test_submit_to_queue_silencieux_si_desactive(app, owner_id, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(QueueStore, "notify_queue", lambda: calls.__setitem__("n", calls["n"] + 1))
    with app.app_context():
        job = JobStore.create_job(owner_id, "Notify off")
        _scheduler(app, use_listen_notify=False).submit_to_queue(job.id, mode="fast")
    assert calls["n"] == 0

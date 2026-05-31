"""Verrou consultatif « ordonnanceur unique » (Phase B / C1)."""
from __future__ import annotations

import types

from transcria.database import db
from transcria.queue.scheduler_lock import SchedulerLock

# Clé de test dédiée : le scheduler global de la fixture `app` détient déjà la clé
# par défaut pendant toute la session — on en prend une autre pour tester en isolation.
_TEST_KEY = 0x5151_2727


def test_advisory_lock_single_holder_then_released(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            import pytest
            pytest.skip("Verrou consultatif : PostgreSQL uniquement")

        first = SchedulerLock(db.engine, key=_TEST_KEY)
        second = SchedulerLock(db.engine, key=_TEST_KEY)
        try:
            assert first.try_acquire() is True
            assert first.acquired is True
            # Un second prétendant échoue tant que le premier tient le verrou.
            assert second.try_acquire() is False
            assert second.acquired is False

            # Après libération, le verrou est de nouveau disponible.
            first.release()
            assert first.acquired is False
            assert second.try_acquire() is True
        finally:
            first.release()
            second.release()


def test_advisory_lock_reacquire_is_idempotent(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            import pytest
            pytest.skip("Verrou consultatif : PostgreSQL uniquement")
        lock = SchedulerLock(db.engine, key=_TEST_KEY + 1)
        try:
            assert lock.try_acquire() is True
            assert lock.try_acquire() is True   # déjà détenu → toujours True, pas de 2e connexion
        finally:
            lock.release()
        # release idempotent
        lock.release()


def test_advisory_lock_noop_on_non_postgres():
    """Dialecte sans verrou consultatif (SQLite, dev) : toujours « acquis »
    (l'unicité y repose sur le mono-process). Aucune connexion réelle ouverte."""
    fake_engine = types.SimpleNamespace(dialect=types.SimpleNamespace(name="sqlite"))
    lock = SchedulerLock(fake_engine)  # type: ignore[arg-type]
    assert lock.try_acquire() is True
    assert lock.acquired is True
    lock.release()
    assert lock.acquired is False

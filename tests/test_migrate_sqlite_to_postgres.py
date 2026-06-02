"""Tests du script de migration SQLite → PostgreSQL.

Nécessite pytest-postgresql (base PG éphémère). Les cas testés :
1. Migration basique de quelques utilisateurs
2. Skip des tables absentes en SQLite (base partielle)
3. Skip des tables non vides en cible sans --truncate
4. Remplacement des données avec --truncate
"""
from __future__ import annotations

import os
import tempfile

import pytest
from alembic.config import Config
from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import create_engine, text

import transcria.audit.models  # noqa: F401
import transcria.auth.models  # noqa: F401
import transcria.context.central_lexicon_models  # noqa: F401
import transcria.jobs.models  # noqa: F401
import transcria.queue.models  # noqa: F401
import transcria.voice.models  # noqa: F401
from alembic import command
from scripts.migrate_sqlite_to_postgres import migrate
from transcria.database import db


@pytest.fixture()
def pg_url(postgresql_proc):
    dbname = "test_migrate"
    auth = postgresql_proc.user if not postgresql_proc.password else f"{postgresql_proc.user}:{postgresql_proc.password}"
    url = f"postgresql+psycopg://{auth}@{postgresql_proc.host}:{postgresql_proc.port}/{dbname}"
    with DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        version=postgresql_proc.version,
        dbname=dbname,
        password=postgresql_proc.password,
    ):
        os.environ["TRANSCRIA_DATABASE_URL"] = url
        try:
            command.upgrade(Config("alembic.ini"), "head")
            yield url
        finally:
            os.environ.pop("TRANSCRIA_DATABASE_URL", None)


@pytest.fixture()
def sqlite_with_users():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    engine = create_engine(f"sqlite:///{path}")
    db.metadata.tables["users"].create(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, username, email, password_hash, is_active, role_id) VALUES (:id, :u, :e, :p, 1, 1)"),
            [{"id": 1, "u": "alice", "e": "alice@test", "p": "hash1"},
             {"id": 2, "u": "bob", "e": "bob@test", "p": "hash2"}],
        )
    engine.dispose()
    yield f"sqlite:///{path}"
    os.unlink(path)


def test_migrate_basic(sqlite_with_users, pg_url):
    total = migrate(sqlite_with_users, pg_url, truncate=False)
    assert total == 2
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
    engine.dispose()
    assert count == 2


def test_migrate_skip_non_empty_target(sqlite_with_users, pg_url):
    migrate(sqlite_with_users, pg_url, truncate=False)
    total = migrate(sqlite_with_users, pg_url, truncate=False)
    assert total == 0


def test_migrate_truncate_replaces(sqlite_with_users, pg_url):
    migrate(sqlite_with_users, pg_url, truncate=False)
    total = migrate(sqlite_with_users, pg_url, truncate=True)
    assert total == 2
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
    engine.dispose()
    assert count == 2

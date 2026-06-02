"""Tests du script de migration SQLite → PostgreSQL.

Nécessite pytest-postgresql (base PG éphémère). Les cas testés :
1. Migration basique de quelques utilisateurs
2. Skip des tables absentes en SQLite (base partielle)
3. Skip des tables non vides en cible sans --truncate
4. Remplacement des données avec --truncate
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from datetime import datetime, timezone

import pytest
from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import create_engine, text

import transcria.audit.models  # noqa: F401
import transcria.auth.models  # noqa: F401
import transcria.context.central_lexicon_models  # noqa: F401
import transcria.jobs.models  # noqa: F401
import transcria.queue.models  # noqa: F401
import transcria.voice.models  # noqa: F401
from alembic import command
from alembic.config import Config
from transcria.database import db

# Charger le script de migration directement depuis le fichier (scripts/ n'est pas un package)
_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "migrate_sqlite_to_postgres.py")
_spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", _SCRIPT_PATH)
_migrate_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migrate_module)
migrate = _migrate_module.migrate


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
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, email, password_hash, role, is_active, created_at) "
                "VALUES (:id, :u, :dn, :e, :p, :r, 1, :now)"
            ),
            [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "u": "alice",
                    "dn": "Alice",
                    "e": "alice@test",
                    "p": "hash1",
                    "r": "admin",
                    "now": now,
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "u": "bob",
                    "dn": "Bob",
                    "e": "bob@test",
                    "p": "hash2",
                    "r": "operator",
                    "now": now,
                },
            ],
        )
    engine.dispose()
    yield f"sqlite:///{path}"
    os.unlink(path)


@pytest.fixture()
def sqlite_with_legacy_users():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    engine = create_engine(f"sqlite:///{path}")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id VARCHAR(36) PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    is_active BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    last_login DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at) "
                "VALUES (:id, :u, :p, :r, 1, :now)"
            ),
            {"id": "33333333-3333-3333-3333-333333333333", "u": "legacy", "p": "hash3", "r": "viewer", "now": now},
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


def test_migrate_legacy_users_missing_new_columns(sqlite_with_legacy_users, pg_url):
    total = migrate(sqlite_with_legacy_users, pg_url, truncate=False)
    assert total == 1
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT username, display_name, email FROM users WHERE username = 'legacy'")).mappings().one()
    engine.dispose()
    assert row == {"username": "legacy", "display_name": "", "email": ""}


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

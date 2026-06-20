"""Tests unitaires de la phase « base PostgreSQL » (chemin post-connexion).

Connexion, lectures d'état, Alembic, reconstruction privilégiée et migration SQLite
sont tous injectés : on vérifie l'orchestration (connexion ko, encodage, keep /
upgrade-existing / create, rebuild local vs échec distant, migration migrate / skip /
prompt) sans base réelle ni alembic. Seule l'écriture du DSN dans `.env` est réelle.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from transcria.installer.console import Console
from transcria.installer.postgres_phase import PostgresPhaseError, PostgresPlan, apply_postgres


def _console() -> Console:
    return Console(io.StringIO(), color=False)


class _FakeQuery:
    """Simule `pg_app_psql -At -c <sql>` : renvoie un scalaire str ou None par requête."""

    def __init__(self, *, connect=True, encoding="UTF8", tables=0, users=0, alembic=""):
        self.connect = connect
        self.encoding = encoding
        self.tables = tables
        self.users = users
        self.alembic = alembic

    def __call__(self, dsn: str, sql: str):
        if sql == "SELECT 1":
            return "1" if self.connect else None
        if "pg_encoding_to_char" in sql:
            return self.encoding or None
        if "information_schema.tables" in sql:
            return str(self.tables)
        if "FROM users" in sql:
            return str(self.users)
        if "alembic_version" in sql:
            return self.alembic or None
        raise AssertionError(f"requête inattendue : {sql!r}")


class _Recorder:
    """Callable enregistrant ses appels et renvoyant un code retour configurable."""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.returncode


def _plan(tmp_path: Path, **kw) -> PostgresPlan:
    defaults = dict(
        host="db.example.org",
        port="5432",
        db="transcria",
        user="transcria_app",
        password="s3cr3t",
        install_dir=tmp_path,
        venv_python=tmp_path / "venv" / "bin" / "python",
        env_file=tmp_path / ".env",
        sqlite_db=tmp_path / "instance" / "transcrIA.db",
        backup_dir=tmp_path / "backups",
        service_user="",
        local_pg=False,
        non_interactive=True,
        pg_migrate=False,
        backup_suffix="20260620_000000",
    )
    defaults.update(kw)
    return PostgresPlan(**defaults)


def test_connection_failure_raises_and_renders(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(PostgresPhaseError):
        apply_postgres(plan, console=_console(), query=_FakeQuery(connect=False))
    # Aucun DSN écrit puisque la connexion a échoué avant.
    assert not plan.env_file.exists()


def test_create_path_writes_dsn_and_runs_alembic(tmp_path):
    plan = _plan(tmp_path)
    alembic = _Recorder(returncode=0)

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=0, users=0),
        alembic_upgrade=alembic, migrate=lambda dsn, suffix: (0, ""),
    )

    assert plan.env_file.is_file()
    content = plan.env_file.read_text()
    assert "TRANSCRIA_DATABASE_URL=" in content
    assert "transcria_app" in content and "transcria" in content
    assert len(alembic.calls) == 1  # create → un seul upgrade
    assert "connection-ok" in result.actions and "create-ok" in result.actions


def test_keep_path_skips_alembic(tmp_path):
    plan = _plan(tmp_path)
    alembic = _Recorder(returncode=0)

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=12, users=3, alembic="abc123"),
        alembic_upgrade=alembic,
    )

    assert alembic.calls == []  # base peuplée → conservation
    assert "schema-keep" in result.actions


def test_upgrade_existing_local_rebuild(tmp_path):
    plan = _plan(tmp_path, local_pg=True)
    admin = _Recorder(returncode=0)

    # Premier upgrade échoue, le second (après DROP/CREATE SCHEMA) réussit.
    calls = {"n": 0}

    def alembic_upgrade(dsn):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else 0

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=8, users=0),
        alembic_upgrade=alembic_upgrade, admin_psql=admin,
    )

    assert calls["n"] == 2  # upgrade, puis re-upgrade après rebuild
    assert len(admin.calls) == 1  # DROP/CREATE SCHEMA privilégié
    assert "rebuild-ok" in result.actions


def test_upgrade_existing_remote_failure_raises(tmp_path):
    plan = _plan(tmp_path, local_pg=False)
    admin = _Recorder(returncode=0)

    with pytest.raises(PostgresPhaseError):
        apply_postgres(
            plan, console=_console(),
            query=_FakeQuery(tables=8, users=0),
            alembic_upgrade=lambda dsn: 1, admin_psql=admin,
        )
    assert admin.calls == []  # distant : aucune reconstruction privilégiée


def test_non_utf8_encoding_emits_warning(tmp_path):
    plan = _plan(tmp_path)
    out = io.StringIO()
    console = Console(out, color=False)

    apply_postgres(
        plan, console=console,
        query=_FakeQuery(encoding="LATIN1", tables=0, users=0),
        alembic_upgrade=lambda dsn: 0,
    )
    assert "LATIN1" in out.getvalue()


def test_sqlite_migration_runs_when_requested(tmp_path):
    sqlite = tmp_path / "instance" / "transcrIA.db"
    sqlite.parent.mkdir(parents=True)
    sqlite.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
    plan = _plan(tmp_path, sqlite_db=sqlite, pg_migrate=True, non_interactive=True)
    migrate_calls: list = []

    def migrate(dsn, suffix):
        migrate_calls.append((dsn, suffix))
        return 0, "OK:Données migrées\n"

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=0, users=0),
        alembic_upgrade=lambda dsn: 0, migrate=migrate,
    )
    assert migrate_calls and migrate_calls[0][1] == "20260620_000000"
    assert "sqlite-migrated" in result.actions


def test_sqlite_migration_prompt_declined(tmp_path):
    sqlite = tmp_path / "instance" / "transcrIA.db"
    sqlite.parent.mkdir(parents=True)
    sqlite.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
    plan = _plan(tmp_path, sqlite_db=sqlite, non_interactive=False)

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=0, users=0),
        alembic_upgrade=lambda dsn: 0,
        migrate=lambda dsn, suffix: (0, ""),
        confirm=lambda: False,
    )
    assert "sqlite-ignored" in result.actions


def test_sqlite_migration_skipped_non_interactive_without_flag(tmp_path):
    sqlite = tmp_path / "instance" / "transcrIA.db"
    sqlite.parent.mkdir(parents=True)
    sqlite.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
    plan = _plan(tmp_path, sqlite_db=sqlite, non_interactive=True, pg_migrate=False)

    result = apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=0, users=0),
        alembic_upgrade=lambda dsn: 0,
        migrate=lambda dsn, suffix: (0, ""),
    )
    assert "sqlite-skip" in result.actions


def test_dsn_written_with_owner_only_permissions(tmp_path):
    plan = _plan(tmp_path)
    apply_postgres(
        plan, console=_console(),
        query=_FakeQuery(tables=0, users=0),
        alembic_upgrade=lambda dsn: 0,
    )
    mode = plan.env_file.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)

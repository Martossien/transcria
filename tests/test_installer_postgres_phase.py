"""Tests unitaires de la phase « base PostgreSQL » (chemin post-connexion).

Connexion, lectures d'état, Alembic, reconstruction privilégiée et migration SQLite
sont tous injectés : on vérifie l'orchestration (connexion ko, encodage, keep /
upgrade-existing / create, rebuild local vs échec distant, migration migrate / skip /
prompt) sans base réelle ni alembic. Seule l'écriture du DSN dans `.env` est réelle.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from transcria.installer.console import Console
from transcria.installer.postgres_phase import (
    PostgresBootstrapPlan,
    PostgresPhaseError,
    PostgresPlan,
    apply_postgres,
    apply_postgres_bootstrap,
)


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


# ── Bootstrap local privilégié (apply_postgres_bootstrap) ───────────────────


class _AdminPsql:
    """Simule `sudo -u postgres psql` : route par args/stdin et enregistre les appels."""

    def __init__(self, *, hba_path="", db_exists="", role_rc=0, db_rcs=(0,)):
        self.hba_path = hba_path
        self.db_exists = db_exists
        self.role_rc = role_rc
        self._db_rcs = iter(db_rcs)
        self.calls: list[tuple[list[str], "str | None"]] = []

    def __call__(self, args, *, stdin=None):
        self.calls.append((list(args), stdin))
        joined = " ".join(args)
        if "SHOW hba_file" in joined:
            return (0, self.hba_path)
        if stdin and "CREATE ROLE" in stdin:
            return (self.role_rc, "")
        if "pg_database" in joined:
            return (0, self.db_exists)
        if stdin and "CREATE DATABASE" in stdin:
            return (next(self._db_rcs), "")
        raise AssertionError(f"appel admin_psql inattendu : {args} stdin={stdin!r}")


def _bootstrap_plan(tmp_path: Path, **kw) -> PostgresBootstrapPlan:
    defaults = dict(db="transcria", user="transcria_app", password="s3cr3t", install_dir=tmp_path)
    defaults.update(kw)
    return PostgresBootstrapPlan(**defaults)


def _noop_rewrite(_path):
    return (0, "changed=0")


def test_bootstrap_happy_path_creates_role_and_database(tmp_path):
    admin = _AdminPsql(db_exists="")  # base absente → création
    result = apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=_console(),
        admin_psql=admin, admin_pg_hba_rewrite=_noop_rewrite, reload_service=lambda: None,
    )
    assert "local-ready" in result.actions
    # le SQL rôle et le SQL base ont bien été pipés via stdin
    assert any(stdin and "CREATE ROLE" in stdin for _, stdin in admin.calls)
    assert any(stdin and "CREATE DATABASE" in stdin for _, stdin in admin.calls)


def test_bootstrap_existing_database_is_not_recreated(tmp_path):
    admin = _AdminPsql(db_exists="1")  # base déjà là
    apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=_console(),
        admin_psql=admin, admin_pg_hba_rewrite=_noop_rewrite, reload_service=lambda: None,
    )
    assert not any(stdin and "CREATE DATABASE" in stdin for _, stdin in admin.calls)


def test_bootstrap_role_failure_raises(tmp_path):
    admin = _AdminPsql(role_rc=1)
    out = io.StringIO()
    with pytest.raises(PostgresPhaseError):
        apply_postgres_bootstrap(
            _bootstrap_plan(tmp_path), console=Console(out, color=False),
            admin_psql=admin, admin_pg_hba_rewrite=_noop_rewrite, reload_service=lambda: None,
        )
    assert "création du rôle" in out.getvalue().lower()


def test_bootstrap_database_falls_back_to_locale_c(tmp_path):
    admin = _AdminPsql(db_exists="", db_rcs=(1, 0))  # UTF8 refusé puis repli C accepté
    out = io.StringIO()
    result = apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=Console(out, color=False),
        admin_psql=admin, admin_pg_hba_rewrite=_noop_rewrite, reload_service=lambda: None,
    )
    assert "local-ready" in result.actions
    create_calls = [stdin for _, stdin in admin.calls if stdin and "CREATE DATABASE" in stdin]
    assert len(create_calls) == 2  # tentative UTF8 puis repli
    assert "LC_COLLATE" in create_calls[1]


def test_bootstrap_database_double_failure_raises(tmp_path):
    admin = _AdminPsql(db_exists="", db_rcs=(1, 1))
    with pytest.raises(PostgresPhaseError):
        apply_postgres_bootstrap(
            _bootstrap_plan(tmp_path), console=_console(),
            admin_psql=admin, admin_pg_hba_rewrite=_noop_rewrite, reload_service=lambda: None,
        )


def test_bootstrap_pg_hba_change_triggers_reload(tmp_path):
    hba = tmp_path / "pg_hba.conf"
    hba.write_text("host all all 127.0.0.1/32 ident\n", encoding="utf-8")
    admin = _AdminPsql(hba_path=str(hba), db_exists="1")
    reloaded: list[bool] = []
    result = apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=_console(),
        admin_psql=admin, admin_pg_hba_rewrite=lambda p: (0, "changed=2"),
        reload_service=lambda: reloaded.append(True),
    )
    assert reloaded == [True]
    assert "pg_hba-reloaded" in result.actions


def test_bootstrap_pg_hba_no_change_no_reload(tmp_path):
    hba = tmp_path / "pg_hba.conf"
    hba.write_text("x\n", encoding="utf-8")
    admin = _AdminPsql(hba_path=str(hba), db_exists="1")
    reloaded: list[bool] = []
    apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=_console(),
        admin_psql=admin, admin_pg_hba_rewrite=lambda p: (0, "changed=0"),
        reload_service=lambda: reloaded.append(True),
    )
    assert reloaded == []


def test_bootstrap_pg_hba_invalid_result_raises(tmp_path):
    hba = tmp_path / "pg_hba.conf"
    hba.write_text("x\n", encoding="utf-8")
    admin = _AdminPsql(hba_path=str(hba), db_exists="1")
    with pytest.raises(PostgresPhaseError):
        apply_postgres_bootstrap(
            _bootstrap_plan(tmp_path), console=_console(),
            admin_psql=admin, admin_pg_hba_rewrite=lambda p: (0, "garbage"), reload_service=lambda: None,
        )


def test_bootstrap_pg_hba_rewrite_failure_warns_and_continues(tmp_path):
    hba = tmp_path / "pg_hba.conf"
    hba.write_text("x\n", encoding="utf-8")
    admin = _AdminPsql(hba_path=str(hba), db_exists="1")
    out = io.StringIO()
    # rewrite échoue (rc!=0) → avertit, ne lève pas, poursuit rôle/base.
    result = apply_postgres_bootstrap(
        _bootstrap_plan(tmp_path), console=Console(out, color=False),
        admin_psql=admin, admin_pg_hba_rewrite=lambda p: (1, ""), reload_service=lambda: None,
    )
    assert "local-ready" in result.actions
    assert "pg_hba.conf" in out.getvalue()


def test_bootstrap_integration_creates_role_and_db_on_real_cluster(postgresql_proc, tmp_path):
    """Intégration : crée réellement le rôle + la base sur le cluster PostgreSQL éphémère.

    admin_psql est injecté pour parler au cluster en superuser (la maintenance db
    `postgres`), exactement comme `sudo -u postgres psql` en prod. Couvre le SQL rôle/base
    de bout en bout (le seul morceau du bootstrap qui touche une vraie PostgreSQL).
    """
    psql = shutil.which("psql")
    if not psql:
        pytest.skip("client psql requis pour l'intégration bootstrap")

    role = f"transcria_bs_{uuid.uuid4().hex[:10]}"
    db = f"transcria_bsdb_{uuid.uuid4().hex[:10]}"

    def admin_psql(args, *, stdin=None):
        cmd = [psql, "-h", postgresql_proc.host, "-p", str(postgresql_proc.port),
               "-U", postgresql_proc.user, "-d", "postgres", *args]
        env = {**os.environ, "PGPASSWORD": postgresql_proc.password or ""}
        cp = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, check=False)
        return (cp.returncode, cp.stdout)

    plan = _bootstrap_plan(tmp_path, db=db, user=role, password="bootstrap-pw-123")
    try:
        result = apply_postgres_bootstrap(
            plan, console=_console(),
            admin_psql=admin_psql, admin_pg_hba_rewrite=lambda p: (0, "changed=0"), reload_service=lambda: None,
        )
        assert "local-ready" in result.actions
        _, has_role = admin_psql(["-At", "-c", f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'"])
        assert has_role.strip() == "1"
        _, has_db = admin_psql(["-At", "-c", f"SELECT 1 FROM pg_database WHERE datname = '{db}'"])
        assert has_db.strip() == "1"
    finally:
        admin_psql(["-c", f'DROP DATABASE IF EXISTS "{db}"'])
        admin_psql(["-c", f'DROP ROLE IF EXISTS "{role}"'])

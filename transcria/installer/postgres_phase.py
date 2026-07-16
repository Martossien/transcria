"""Phase « base PostgreSQL » de l'installateur (chemin post-connexion).

Quatrième tranche fondue depuis `install.sh` (SECTION 6.5, `_setup_postgres`). Couvre
le **chemin commun** exécuté une fois la base joignable : test de connexion, garde
d'encodage UTF8, écriture du DSN dans `.env`, détection de l'état (schéma / données /
version Alembic), décision + exécution Alembic (`keep` / `upgrade-existing` / `create`,
avec reconstruction locale privilégiée en repli) et migration SQLite → PostgreSQL.

Le **bootstrap local privilégié** (réécriture de `pg_hba.conf`, création du rôle et de
la base via `sudo -u postgres`, reload du service) reste volontairement dans
`install.sh` : il change d'identité système et n'est pas couvert par le filet E2E. Ce
même filet exerce en revanche intégralement ce chemin-ci via `--pg-existing`.

Choix tourné Docker : la détection d'état et le test de connexion passent par
SQLAlchemy/psycopg (dépendance dure de l'application) plutôt que par le client `psql`.
Le chemin « base existante / distante » n'exige donc plus le binaire `psql` — seul le
bootstrap local (resté en shell) en dépend. Cette phase tourne sous le python du venv.

Les messages reprennent au mot près `transcria.installer.postgres_lib` (texte audité, testé).
"""
from __future__ import annotations

import io
import os
import subprocess
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from transcria.installer.messages import t
from transcria.installer.postgres_lib import (
    build_pg_dsn,
    decide_schema_action,
    decide_sqlite_migration_action,
    human_file_size,
    parse_non_negative_int,
    render_alembic_log,
    render_connection_failure,
    render_database_sql,
    render_encoding_warnings,
    render_pg_hba_rewrite_result,
    render_role_sql,
    render_schema_action_log,
    render_setup_log,
    render_sqlite_migration_log,
    render_sqlite_migration_prompt,
    render_state_query,
    render_state_summary,
    run_sqlite_migration,
)

Query = Callable[[str, str], "str | None"]
AlembicUpgrade = Callable[[str], int]
AdminPsql = Callable[[str], int]
Migrate = Callable[[str, str], "tuple[int, str]"]
Confirm = Callable[[], bool]
Chown = Callable[[Path, str], None]
# Bootstrap local privilégié : (args, stdin) -> (returncode, stdout).
AdminPsqlIO = Callable[..., "tuple[int, str]"]
AdminPgHbaRewrite = Callable[[str], "tuple[int, str]"]
ReloadService = Callable[[], None]


class PostgresPhaseError(RuntimeError):
    """Échec bloquant de la phase PostgreSQL (le message a déjà été rendu à la console)."""


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class PostgresPlan:
    host: str
    port: str
    db: str
    user: str
    password: str
    install_dir: Path
    venv_python: Path
    env_file: Path
    sqlite_db: Path
    backup_dir: Path
    service_user: str = ""
    local_pg: bool = False
    non_interactive: bool = True
    pg_migrate: bool = False
    # `pg_defer` : écrire le DSN SANS se connecter ni migrer (schéma déféré au runtime, job
    # `migrate`). Indispensable pour un BUILD D'IMAGE HERMÉTIQUE : `docker build` n'a pas de
    # base live, et un build d'image ne doit jamais dépendre d'un service externe. Le DSN baké
    # est sans effet au runtime (`resolve_database_uri` priorise `TRANSCRIA_DATABASE_URL`).
    pg_defer: bool = False
    is_root: bool = False
    admin_psql_cmd: tuple[str, ...] = ()  # préfixe psql privilégié (rebuild local) ; vide = indisponible
    backup_suffix: str | None = None  # injectable pour des tests déterministes


@dataclass
class PostgresResult:
    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.actions.append(action)


_TAGS = ("OK", "INFO", "WARN", "ERROR")


def _emit_text(console: _ConsoleLike, text: str) -> None:
    """Route chaque ligne rendue vers la console selon son préfixe (OK/INFO/WARN/ERROR).

    Une ligne sans préfixe reconnu (ex. le résumé d'état, qui contient des « : »
    internes) est affichée telle quelle en INFO — on ne la tronque jamais.
    """
    methods = {"OK": console.ok, "INFO": console.info, "WARN": console.warn, "ERROR": console.error}
    for line in text.splitlines():
        if not line:
            continue
        tag, sep, rest = line.partition(":")
        if sep and tag in _TAGS:
            methods[tag](rest)
        else:
            console.info(line)


def _scalar_int(value: "str | None") -> int:
    if value is None:
        return 0
    try:
        return parse_non_negative_int(value, name="count")
    except ValueError:
        return 0


def _default_query(dsn: str, sql: str) -> "str | None":
    """Lit un scalaire via SQLAlchemy ; toute erreur (table absente, connexion) → None.

    Reproduit fidèlement `psql -At -c … 2>/dev/null || défaut` : une requête qui échoue
    (schéma ou données manquants) renvoie None, traité comme « absent » par l'appelant.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql)).first()
    except Exception:
        return None
    finally:
        engine.dispose()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _default_alembic_upgrade(plan: PostgresPlan) -> AlembicUpgrade:
    def upgrade(dsn: str) -> int:
        alembic_bin = plan.venv_python.parent / "alembic"
        env = {**os.environ, "TRANSCRIA_DATABASE_URL": dsn}
        return subprocess.run([str(alembic_bin), "upgrade", "head"], env=env, check=False).returncode

    return upgrade


def _default_admin_psql(plan: PostgresPlan) -> AdminPsql:
    def run(db: str) -> int:
        if not plan.admin_psql_cmd:
            return 127
        cmd = [*plan.admin_psql_cmd, "-d", db, "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"]
        return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode

    return run


def _default_migrate(plan: PostgresPlan) -> Migrate:
    def migrate(dsn: str, suffix: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = run_sqlite_migration(
                dsn=dsn,
                sqlite_db=plan.sqlite_db,
                backup_dir=plan.backup_dir,
                suffix=suffix,
                install_dir=plan.install_dir,
                python_bin=str(plan.venv_python),
            )
        return rc, buffer.getvalue()

    return migrate


def _default_confirm() -> bool:
    try:
        return input().strip() == "1"
    except EOFError:
        return False


def _best_effort_chown(path: Path, service_user: str) -> None:
    """chown best-effort du `.env` vers l'utilisateur du service (fidèle à secure_env_file)."""
    import shutil

    try:
        shutil.chown(path, user=service_user)
    except (LookupError, PermissionError, OSError):
        pass


def _write_dsn(plan: PostgresPlan, console: _ConsoleLike, dsn: str, chown: Chown, result: PostgresResult) -> None:
    # Différé (§8.3 c — point d'entrée pré-venv) : installer.cli tourne avec le python
    # SYSTÈME avant requirements ; le `__init__` de transcria.config exécute loader → yaml.
    from transcria.config.env_file import update_env_file

    # update_env_file écrit déjà en 0o600 (atomic_write_text) ; le chown service reproduit
    # secure_env_file pour que le service systemd (souvent root) lise le DSN.
    update_env_file(plan.env_file, "TRANSCRIA_DATABASE_URL", dsn, backup=False)
    if plan.is_root and plan.service_user:
        chown(plan.env_file, plan.service_user)
    _emit_text(console, render_setup_log(event="dsn-written", db=plan.db, user=plan.user, host=plan.host))
    result.record("dsn-written")


def _apply_alembic(plan: PostgresPlan, console: _ConsoleLike, dsn: str, action: str,
                   alembic_upgrade: AlembicUpgrade, admin_psql: AdminPsql, result: PostgresResult) -> None:
    _emit_text(console, render_schema_action_log(db=plan.db, action=action))
    if action == "keep":
        result.record("schema-keep")
        return

    if alembic_upgrade(dsn) == 0:
        _emit_text(console, render_alembic_log(event="create-ok" if action == "create" else "upgrade-ok"))
        result.record(f"{action}-ok")
        return

    if action == "create":
        _emit_text(console, render_alembic_log(event="create-failed"))
        raise PostgresPhaseError("alembic create failed")

    # upgrade-existing en échec : reconstruction locale privilégiée, sinon abandon distant.
    if not plan.local_pg:
        _emit_text(console, render_alembic_log(event="remote-upgrade-failed"))
        raise PostgresPhaseError("remote alembic upgrade failed")

    _emit_text(console, render_alembic_log(event="rebuild-start"))
    admin_psql(plan.db)  # DROP/CREATE SCHEMA ; échec ignoré (cf. install.sh : `|| true`)
    if alembic_upgrade(dsn) == 0:
        _emit_text(console, render_alembic_log(event="rebuild-ok"))
        result.record("rebuild-ok")
        return
    _emit_text(console, render_alembic_log(event="rebuild-failed"))
    raise PostgresPhaseError("local rebuild failed")


def _maybe_migrate_sqlite(plan: PostgresPlan, console: _ConsoleLike, dsn: str, has_data: int,
                          migrate: Migrate, confirm: Confirm, result: PostgresResult) -> None:
    sqlite_present = plan.sqlite_db.is_file() and plan.sqlite_db.stat().st_size > 0
    action = decide_sqlite_migration_action(
        sqlite_present=sqlite_present,
        has_data=has_data,
        non_interactive=plan.non_interactive,
        pg_migrate=plan.pg_migrate,
    )
    if action == "none":
        return

    suffix = plan.backup_suffix or datetime.now().strftime("%Y%m%d_%H%M%S")
    _emit_text(console, render_sqlite_migration_log(event="detected", sqlite_db=str(plan.sqlite_db)))

    if action == "skip":
        _emit_text(console, render_sqlite_migration_log(event="skipped", sqlite_db=str(plan.sqlite_db)))
        result.record("sqlite-skip")
        return

    if action == "prompt":
        print(render_sqlite_migration_prompt(
            sqlite_db=str(plan.sqlite_db),
            sqlite_size=human_file_size(plan.sqlite_db),
            db=plan.db, host=plan.host, port=str(plan.port),
        ), end="")
        if not confirm():
            _emit_text(console, render_sqlite_migration_log(event="ignored", sqlite_db=str(plan.sqlite_db)))
            result.record("sqlite-ignored")
            return

    rc, output = migrate(dsn, suffix)
    _emit_text(console, output)
    if rc != 0:
        raise PostgresPhaseError("sqlite migration failed")
    result.record("sqlite-migrated")


def apply_postgres(
    plan: PostgresPlan,
    *,
    console: _ConsoleLike,
    query: Query = _default_query,
    alembic_upgrade: AlembicUpgrade | None = None,
    admin_psql: AdminPsql | None = None,
    migrate: Migrate | None = None,
    confirm: Confirm = _default_confirm,
    chown: Chown = _best_effort_chown,
) -> PostgresResult:
    """Orchestre le chemin post-connexion PostgreSQL (cf. docstring du module)."""
    result = PostgresResult()
    alembic_upgrade = alembic_upgrade or _default_alembic_upgrade(plan)
    admin_psql = admin_psql or _default_admin_psql(plan)
    migrate = migrate or _default_migrate(plan)

    dsn = build_pg_dsn(plan.host, plan.port, plan.db, plan.user, plan.password)

    # ── Mode différé (build d'image hermétique) ───────────────
    # On écrit le DSN et on s'arrête : aucune connexion, aucun schéma, aucune migration.
    # Le job `migrate` (runtime) appliquera `alembic upgrade head` contre la VRAIE base.
    if plan.pg_defer:
        _emit_text(console, "INFO:PostgreSQL : DSN écrit, schéma DÉFÉRÉ au runtime (--pg-defer) — "
                            "pas de connexion ni d'Alembic au build.")
        _write_dsn(plan, console, dsn, chown, result)
        result.record("deferred")
        return result

    # ── Test de connexion ─────────────────────────────────────
    if query(dsn, "SELECT 1") != "1":
        _emit_text(console, render_connection_failure(
            db=plan.db, user=plan.user, host=plan.host, port=str(plan.port), local_pg=plan.local_pg,
        ))
        raise PostgresPhaseError("connection failed")
    _emit_text(console, render_setup_log(event="connection-ok", db=plan.db, user=plan.user, host=plan.host))
    result.record("connection-ok")

    # ── Garde encodage UTF8 ───────────────────────────────────
    encoding = query(dsn, render_state_query("encoding")) or ""
    warnings = render_encoding_warnings(plan.db, encoding)
    if warnings.strip():
        _emit_text(console, "\n".join(f"WARN:{line}" for line in warnings.splitlines() if line))

    # ── DSN dans .env ─────────────────────────────────────────
    _write_dsn(plan, console, dsn, chown, result)

    # ── État de la base ───────────────────────────────────────
    has_schema = _scalar_int(query(dsn, render_state_query("public-table-count")))
    has_data = _scalar_int(query(dsn, render_state_query("users-count")))
    alembic_version = query(dsn, render_state_query("alembic-version")) or ""
    _emit_text(console, render_state_summary(
        db=plan.db, has_schema=has_schema, has_data=has_data, alembic_version=alembic_version,
    ))

    # ── Schéma Alembic ────────────────────────────────────────
    _apply_alembic(plan, console, dsn, decide_schema_action(has_schema, has_data), alembic_upgrade, admin_psql, result)

    # ── Migration SQLite (si base vide et SQLite présent) ─────
    _maybe_migrate_sqlite(plan, console, dsn, has_data, migrate, confirm, result)

    return result


# ── Bootstrap local privilégié (rôle/base/pg_hba) ───────────────────────────
# Provisionnement d'une PostgreSQL *locale* : réécriture de pg_hba.conf pour
# l'authentification TCP par mot de passe, création idempotente du rôle et de la base
# (UTF8 imposé, repli locale C). Tout passe par l'identité système `postgres`
# (`sudo -u postgres` / `runuser`), d'où des callables privilégiés injectables. Ce
# chemin n'est PAS couvert par le filet E2E (qui utilise `--pg-existing`) ; le SQL est
# vérifié par les tests des renderers (`tests/test_install_postgres.py`) et l'orchestration
# par `tests/test_installer_postgres_phase.py` (séquence + branches, avec un test
# d'intégration création rôle/base contre le cluster éphémère).


@dataclass(frozen=True)
class PostgresBootstrapPlan:
    db: str
    user: str
    password: str
    install_dir: Path
    host: str = "127.0.0.1"
    port: str = "5432"
    is_root: bool = False
    have_systemctl: bool = False
    have_service: bool = False
    admin_psql_cmd: tuple[str, ...] = ()    # ex. ("sudo", "-u", "postgres", "psql")
    admin_python_cmd: tuple[str, ...] = ()  # ex. ("sudo", "-u", "postgres", "env", "PYTHONPATH=…", "python", "-m")


def _default_admin_psql_io(plan: PostgresBootstrapPlan) -> AdminPsqlIO:
    def run(args: list[str], *, stdin: str | None = None) -> tuple[int, str]:
        if not plan.admin_psql_cmd:
            return (127, "")
        cp = subprocess.run([*plan.admin_psql_cmd, *args], input=stdin, capture_output=True, text=True, check=False)
        return (cp.returncode, cp.stdout)

    return run


def _default_admin_pg_hba_rewrite(plan: PostgresBootstrapPlan) -> AdminPgHbaRewrite:
    def run(path: str) -> tuple[int, str]:
        if not plan.admin_python_cmd:
            return (127, "")
        cp = subprocess.run([*plan.admin_python_cmd, "transcria.installer.postgres_lib", path], capture_output=True, text=True, check=False)
        return (cp.returncode, cp.stdout)

    return run


def _default_reload_service(plan: PostgresBootstrapPlan) -> ReloadService:
    def reload() -> None:
        import time

        prefix: list[str] = [] if plan.is_root else ["sudo"]
        if plan.have_systemctl and subprocess.run(["systemctl", "is-active", "--quiet", "postgresql"], check=False).returncode == 0:
            subprocess.run([*prefix, "systemctl", "reload", "postgresql"], check=False)
        elif plan.have_service:
            subprocess.run([*prefix, "service", "postgresql", "reload"], check=False)
        time.sleep(1)

    return reload


def _bootstrap_pg_hba(
    plan: PostgresBootstrapPlan, console: _ConsoleLike,
    admin_psql: AdminPsqlIO, admin_pg_hba_rewrite: AdminPgHbaRewrite, reload_service: ReloadService,
    result: PostgresResult,
) -> None:
    rc, path = admin_psql(["-At", "-c", "SHOW hba_file;"])
    path = path.strip()
    if rc != 0 or not path or not Path(path).is_file():
        return

    hba_rc, raw = admin_pg_hba_rewrite(path)
    if hba_rc != 0:
        console.warn(t("pp_pg_hba_failed"))
        return

    try:
        decision = render_pg_hba_rewrite_result(raw.strip())
    except ValueError:
        console.warn(t("pp_pg_hba_invalid", raw=raw.strip()))
        raise PostgresPhaseError("pg_hba result invalid") from None

    should_reload = False
    for line in decision.splitlines():
        if line.startswith("INFO:"):
            console.info(line[len("INFO:"):])
        elif line == "ACTION:reload":
            should_reload = True
    if should_reload:
        reload_service()
        result.record("pg_hba-reloaded")


def _bootstrap_role(plan: PostgresBootstrapPlan, console: _ConsoleLike, admin_psql: AdminPsqlIO) -> None:
    rc, _ = admin_psql(
        ["-v", "ON_ERROR_STOP=1", "-v", f"role={plan.user}", "-v", f"pwd={plan.password}"],
        stdin=render_role_sql(),
    )
    if rc != 0:
        _emit_text(console, render_setup_log(event="role-error", db=plan.db, user=plan.user, host=plan.host))
        raise PostgresPhaseError("role creation failed")


def _bootstrap_database(plan: PostgresBootstrapPlan, console: _ConsoleLike, admin_psql: AdminPsqlIO) -> None:
    _, exists = admin_psql(["-At", "-v", f"dbname={plan.db}", "-c", render_state_query("database-exists")])
    if exists.strip() == "1":
        return

    db_args = ["-v", "ON_ERROR_STOP=1", "-v", f"dbname={plan.db}", "-v", f"role={plan.user}"]
    rc, _ = admin_psql(db_args, stdin=render_database_sql())
    if rc == 0:
        return
    # Locale du cluster incompatible UTF8 : repli LC_COLLATE/LC_CTYPE 'C'.
    _emit_text(console, render_setup_log(event="database-fallback", db=plan.db, user=plan.user, host=plan.host))
    rc, _ = admin_psql(db_args, stdin=render_database_sql(fallback_locale_c=True))
    if rc != 0:
        _emit_text(console, render_setup_log(event="database-error", db=plan.db, user=plan.user, host=plan.host))
        raise PostgresPhaseError("database creation failed")


def apply_postgres_bootstrap(
    plan: PostgresBootstrapPlan,
    *,
    console: _ConsoleLike,
    admin_psql: AdminPsqlIO | None = None,
    admin_pg_hba_rewrite: AdminPgHbaRewrite | None = None,
    reload_service: ReloadService | None = None,
    app_query: Query = _default_query,
) -> PostgresResult:
    """Provisionne une PostgreSQL locale (pg_hba + rôle + base), via l'identité postgres.

    Court-circuit : si la base est DÉJÀ joignable avec les identifiants applicatifs (rôle +
    base + pg_hba déjà bons), on saute tout le bootstrap privilégié — évite un échec inutile
    (PermissionError pg_hba / sudo) quand on relance l'install sur une base déjà provisionnée.
    """
    result = PostgresResult()

    dsn = build_pg_dsn(plan.host, plan.port, plan.db, plan.user, plan.password)
    if app_query(dsn, "SELECT 1") == "1":
        console.ok(t("pp_already_provisioned", user=plan.user, db=plan.db))
        result.record("already-provisioned")
        return result

    admin_psql = admin_psql or _default_admin_psql_io(plan)
    admin_pg_hba_rewrite = admin_pg_hba_rewrite or _default_admin_pg_hba_rewrite(plan)
    reload_service = reload_service or _default_reload_service(plan)

    _bootstrap_pg_hba(plan, console, admin_psql, admin_pg_hba_rewrite, reload_service, result)

    _emit_text(console, render_setup_log(event="local-check", db=plan.db, user=plan.user, host=plan.host))
    _bootstrap_role(plan, console, admin_psql)
    _bootstrap_database(plan, console, admin_psql)

    _emit_text(console, render_setup_log(event="local-ready", db=plan.db, user=plan.user, host=plan.host))
    result.record("local-ready")
    return result

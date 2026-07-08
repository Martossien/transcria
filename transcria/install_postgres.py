from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

_LOCAL_TCP_PEER_RE = re.compile(
    r"^(?P<prefix>\s*host\s+(?:all|replication)\s+all\s+(?:127\.0\.0\.1/32|::1/128)\s+)(?:ident|peer)(?P<suffix>\s*(?:#.*)?)$"
)
_PG_HBA_REWRITE_RESULT_RE = re.compile(r"^changed=(?P<count>[0-9]+)$")
_PG_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_STATE_QUERIES = {
    "database-exists": "SELECT 1 FROM pg_database WHERE datname = :'dbname';",
    "encoding": "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database();",
    "public-table-count": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'",
    "users-count": "SELECT COUNT(*) FROM users",
    "alembic-version": "SELECT version_num FROM alembic_version",
}


def is_local_pg_host(host: str) -> bool:
    """Retourne vrai si l'hôte PostgreSQL désigne la boucle locale."""
    return host in {"127.0.0.1", "localhost", "::1"}


def build_pg_dsn(host: str, port: str | int, db: str, user: str, password: str) -> str:
    """Construit un DSN SQLAlchemy psycopg avec encodage sûr des identifiants."""
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host_part}:{port}/{quote(db, safe='')}"


def backup_sqlite_database(sqlite_path: Path, backup_dir: Path, suffix: str) -> Path:
    """Copie la base SQLite avant migration PostgreSQL et retourne le backup."""
    sqlite_path = Path(sqlite_path)
    backup_dir = Path(backup_dir)
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{sqlite_path.stem}_{suffix}{sqlite_path.suffix}.bak"
    shutil.copy2(sqlite_path, backup_path)
    return backup_path


def human_file_size(path: Path) -> str:
    """Retourne une taille de fichier lisible, sans dépendre de `du`."""
    size = Path(path).stat().st_size
    units = ("B", "K", "M", "G", "T")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            if value >= 10:
                return f"{value:.0f}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def generate_pg_password(nbytes: int = 24) -> str:
    """Génère un mot de passe PostgreSQL URL-safe."""
    return secrets.token_urlsafe(nbytes)


def validate_pg_inputs(db: str, user: str, port: str | int) -> list[str]:
    """Valide les entrées PostgreSQL demandées à l'installation."""
    errors: list[str] = []
    if not _PG_IDENTIFIER_RE.match(db):
        errors.append(f"Nom de base invalide : '{db}' (attendu : [a-zA-Z_][a-zA-Z0-9_]{{0,62}})")
    if not _PG_IDENTIFIER_RE.match(user):
        errors.append(f"Nom de rôle invalide : '{user}' (attendu : [a-zA-Z_][a-zA-Z0-9_]{{0,62}})")
    try:
        port_int = int(str(port))
    except ValueError:
        errors.append(f"Port invalide : '{port}' (attendu : 1-65535)")
    else:
        if str(port) != str(port_int) or not 1 <= port_int <= 65535:
            errors.append(f"Port invalide : '{port}' (attendu : 1-65535)")
    return errors


def parse_non_negative_int(value: str | int, *, name: str) -> int:
    """Parse un compteur PostgreSQL non négatif retourné par psql."""
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} invalide : {value}") from exc
    if parsed < 0:
        raise ValueError(f"{name} négatif invalide : {value}")
    return parsed


def parse_bool(value: str | bool, *, name: str) -> bool:
    """Parse un booléen CLI stable."""
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} booléen invalide : {value}")


def decide_schema_action(has_schema: str | int, has_data: str | int) -> str:
    """Décide l'action Alembic à partir de l'état courant de la base."""
    schema_count = parse_non_negative_int(has_schema, name="has_schema")
    data_count = parse_non_negative_int(has_data, name="has_data")
    if schema_count > 0 and data_count > 0:
        return "keep"
    if schema_count > 0:
        return "upgrade-existing"
    return "create"


def decide_sqlite_migration_action(
    *,
    sqlite_present: str | bool,
    has_data: str | int,
    non_interactive: str | bool,
    pg_migrate: str | bool,
) -> str:
    """Décide si la migration SQLite doit être lancée, sautée ou demandée."""
    if not parse_bool(sqlite_present, name="sqlite_present"):
        return "none"
    if parse_non_negative_int(has_data, name="has_data") > 0:
        return "none"
    if not parse_bool(non_interactive, name="non_interactive"):
        return "prompt"
    return "migrate" if parse_bool(pg_migrate, name="pg_migrate") else "skip"


def render_role_sql() -> str:
    """Rend le SQL idempotent de création/mise à jour du rôle applicatif."""
    return (
        "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pwd')\n"
        "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') \\gexec\n"
        "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role', :'pwd') \\gexec\n"
    )


def render_database_sql(*, fallback_locale_c: bool = False) -> str:
    """Rend le SQL idempotent de création de base UTF8."""
    if fallback_locale_c:
        return (
            "SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L LC_COLLATE %L LC_CTYPE %L TEMPLATE template0',\n"
            "              :'dbname', :'role', 'UTF8', 'C', 'C') \\gexec\n"
        )
    return "SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', :'dbname', :'role', 'UTF8') \\gexec\n"


def render_state_query(name: str) -> str:
    """Rend une requête de lecture d'état PostgreSQL utilisée par install.sh."""
    try:
        return _STATE_QUERIES[name] + "\n"
    except KeyError as exc:
        expected = ", ".join(sorted(_STATE_QUERIES))
        raise ValueError(f"requête inconnue: {name} (attendues: {expected})") from exc


def render_encoding_warnings(db: str, encoding: str) -> str:
    """Rend les avertissements quand une base existante n'est pas en UTF8."""
    encoding = encoding.strip()
    if not encoding or encoding == "UTF8":
        return ""
    return "\n".join([
        f"La base '{db}' existe déjà en encodage {encoding} (UTF8 attendu) :",
        "texte stocké SANS validation d'encodage — migrez-la dès que possible",
        "(procédure : docs/INSTALL.md, section « Encodage de la base »).",
        "L'application force client_encoding=utf8 et reste fonctionnelle en attendant.",
    ]) + "\n"


def render_connection_failure(*, db: str, user: str, host: str, port: str, local_pg: str | bool) -> str:
    """Rend les messages d'échec de connexion PostgreSQL."""
    lines = [
        f"ERROR:Connexion PostgreSQL impossible avec le rôle '{user}' sur '{db}@{host}:{port}'.",
    ]
    if parse_bool(local_pg, name="local_pg"):
        lines.append("WARN:Vérifiez pg_hba.conf et le reload PostgreSQL ; l'authentification TCP doit accepter le mot de passe.")
    else:
        lines.append("WARN:Créez la base et le rôle côté serveur, puis relancez avec --pg-host/--pg-user/--pg-password.")
    return "\n".join(lines) + "\n"


def render_state_summary(*, db: str, has_schema: str | int, has_data: str | int, alembic_version: str) -> str:
    """Rend le résumé d'état PostgreSQL affiché avant décision Alembic."""
    from transcria.install_messages import t

    schema_count = parse_non_negative_int(has_schema, name="has_schema")
    data_count = parse_non_negative_int(has_data, name="has_data")
    return t("pg_state_summary", db=db, schema=schema_count, alembic=alembic_version, data=data_count) + "\n"


def render_schema_action_log(*, db: str, action: str) -> str:
    """Rend le message initial associé à une action Alembic (FR/EN)."""
    from transcria.install_messages import t

    if action == "keep":
        return f"OK:{t('pg_schema_keep', db=db)}\n"
    if action == "upgrade-existing":
        return f"INFO:{t('pg_schema_upgrade', db=db)}\n"
    if action == "create":
        return f"INFO:{t('pg_schema_create')}\n"
    raise ValueError(f"action Alembic PostgreSQL inconnue : {action}")


def render_pg_hba_rewrite_result(result: str) -> str:
    """Interprète le résultat de réécriture pg_hba.conf pour le shell."""
    match = _PG_HBA_REWRITE_RESULT_RE.fullmatch(result.strip())
    if not match:
        raise ValueError(f"résultat pg_hba.conf invalide : {result}")
    changed = int(match.group("count"))
    if changed == 0:
        return "ACTION:none\n"
    from transcria.install_messages import t as _t_hba
    return f"INFO:{_t_hba('pg_hba_update')}\nACTION:reload\n"


def render_setup_log(*, event: str, db: str, user: str, host: str) -> str:
    """Rend les messages de bootstrap PostgreSQL local/distant (FR/EN)."""
    from transcria.install_messages import t

    if event == "local-check":
        return f"INFO:{t('pg_local_check', user=user, db=db)}\n"
    if event == "role-error":
        return f"ERROR:{t('pg_role_error')}\n"
    if event == "database-fallback":
        return f"WARN:{t('pg_database_fallback')}\n"
    if event == "database-error":
        return f"ERROR:{t('pg_database_error')}\n"
    if event == "local-ready":
        return f"OK:{t('pg_local_ready')}\n"
    if event == "remote-detected":
        return f"INFO:{t('pg_remote_detected', host=host)}\n"
    if event == "connection-ok":
        return f"OK:{t('pg_connection_ok')}\n"
    if event == "dsn-written":
        return f"OK:{t('pg_dsn_written')}\n"
    raise ValueError(f"événement PostgreSQL inconnu : {event}")


def render_alembic_log(*, event: str, action: str = "") -> str:
    """Rend les messages de résultat Alembic PostgreSQL (FR/EN)."""
    from transcria.install_messages import t

    if event == "upgrade-ok":
        return f"OK:{t('pg_alembic_upgrade_ok')}\n"
    if event == "rebuild-start":
        return f"ERROR:{t('pg_alembic_rebuild_start')}\n"
    if event == "rebuild-ok":
        return f"OK:{t('pg_alembic_rebuild_ok')}\n"
    if event == "rebuild-failed":
        return f"ERROR:{t('pg_alembic_rebuild_failed')}\n"
    if event == "remote-upgrade-failed":
        return f"ERROR:{t('pg_alembic_remote_failed')}\n"
    if event == "create-ok":
        return f"OK:{t('pg_alembic_create_ok')}\n"
    if event == "create-failed":
        return f"ERROR:{t('pg_alembic_create_failed')}\n"
    if event == "unknown-action":
        return f"ERROR:{t('pg_alembic_unknown', action=action)}\n"
    raise ValueError(f"événement Alembic PostgreSQL inconnu : {event}")


def render_sqlite_migration_log(*, event: str, sqlite_db: str, action: str = "", backup_path: str = "") -> str:
    """Rend les messages liés à la migration SQLite vers PostgreSQL (FR/EN)."""
    from transcria.install_messages import t

    if event == "detected":
        return f"INFO:{t('pg_sqlite_detected', sqlite_db=sqlite_db)}\n"
    if event == "skipped":
        return f"INFO:{t('pg_sqlite_skipped')}\n"
    if event == "ignored":
        return f"INFO:{t('pg_sqlite_ignored', sqlite_db=sqlite_db)}\n"
    if event == "unknown-action":
        return f"ERROR:{t('pg_sqlite_unknown_action', action=action)}\n"
    if event == "backup-error":
        return f"ERROR:{t('pg_sqlite_backup_error', sqlite_db=sqlite_db, backup_path=backup_path)}\n"
    if event == "backup-ok":
        return f"OK:{t('pg_sqlite_backup_ok', backup_path=backup_path)}\n"
    if event == "migrate-start":
        return f"INFO:{t('pg_sqlite_migrate_start')}\n"
    if event == "migrate-ok":
        return f"OK:{t('pg_sqlite_migrate_ok')}\n"
    if event == "migrate-failed":
        return f"ERROR:{t('pg_sqlite_migrate_failed')}\n"
    if event == "migrate-partial":
        return f"WARN:{t('pg_sqlite_migrate_partial')}\n"
    raise ValueError(f"événement de migration SQLite inconnu : {event}")


def render_sqlite_migration_prompt(*, sqlite_db: str, sqlite_size: str, db: str, host: str, port: str) -> str:
    """Rend le prompt interactif de migration SQLite vers PostgreSQL (FR/EN)."""
    from transcria.install_messages import t

    return "\n".join([
        "",
        t("pg_migprompt_title"),
        t("pg_migprompt_source", sqlite_db=sqlite_db, sqlite_size=sqlite_size),
        t("pg_migprompt_target", db=db, host=host, port=port),
        "",
        t("pg_migprompt_options"),
        t("pg_migprompt_opt1"),
        t("pg_migprompt_opt2"),
        t("pg_migprompt_choice"),
    ])


def render_database_setup_log(*, event: str, user: str = "", db: str = "", host: str = "", port: str = "") -> str:
    """Rend les messages du choix global SQLite/PostgreSQL (FR/EN ; commandes dnf/apt littérales)."""
    from transcria.install_messages import t

    if event == "sqlite-kept":
        return f"OK:{t('pg_db_sqlite_kept')}\n"
    if event == "psql-missing":
        return "\n".join([
            f"ERROR:{t('pg_db_psql_missing1')}",
            "WARN:  Fedora/RHEL  : sudo dnf install postgresql-server postgresql && "
            "sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql",
            "WARN:  Debian/Ubuntu: sudo apt install postgresql && sudo systemctl enable --now postgresql",
            f"ERROR:{t('pg_db_stop_not_sqlite')}",
        ]) + "\n"
    if event == "sudo-missing":
        return "\n".join([
            f"ERROR:{t('pg_db_sudo_missing1')}",
            f"ERROR:{t('pg_db_stop_not_sqlite')}",
        ]) + "\n"
    if event == "password-generated":
        return f"INFO:{t('pg_db_password_generated', user=user)}\n"
    if event == "configured":
        return f"VALUE:PostgreSQL ({db}@{host}:{port})\n"
    if event == "config-failed":
        return f"ERROR:{t('pg_db_config_failed')}\n"
    raise ValueError(f"événement de choix base de données inconnu : {event}")


def rewrite_pg_hba_for_tcp_password(content: str) -> tuple[str, int]:
    """Remplace ident/peer par scram-sha-256 pour les connexions TCP locales."""
    changed = 0
    output: list[str] = []
    for line in content.splitlines(keepends=True):
        ending = ""
        body = line
        if body.endswith("\r\n"):
            body = body[:-2]
            ending = "\r\n"
        elif body.endswith("\n"):
            body = body[:-1]
            ending = "\n"
        match = _LOCAL_TCP_PEER_RE.match(body)
        if match:
            output.append(f"{match.group('prefix')}scram-sha-256{match.group('suffix')}{ending}")
            changed += 1
        else:
            output.append(line)
    return "".join(output), changed


def rewrite_pg_hba_file(path: Path) -> int:
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    updated, changed = rewrite_pg_hba_for_tcp_password(original)
    if changed == 0:
        return 0

    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return changed


def run_sqlite_migration(
    *,
    dsn: str,
    sqlite_db: Path,
    backup_dir: Path,
    suffix: str,
    install_dir: Path,
    python_bin: str,
) -> int:
    """Sauvegarde SQLite puis lance la migration SQLite → PostgreSQL."""
    try:
        backup = backup_sqlite_database(sqlite_db, backup_dir, suffix)
    except OSError as exc:
        print(render_sqlite_migration_log(event="backup-error", sqlite_db=str(sqlite_db), backup_path=str(exc)), end="")
        return 1

    print(render_sqlite_migration_log(event="backup-ok", sqlite_db=str(sqlite_db), backup_path=str(backup)), end="")
    print(render_sqlite_migration_log(event="migrate-start", sqlite_db=str(sqlite_db)), end="")
    env = os.environ.copy()
    env["TRANSCRIA_DATABASE_URL"] = dsn
    result = subprocess.run(
        [
            python_bin,
            str(Path(install_dir) / "scripts" / "migrate_sqlite_to_postgres.py"),
            "--source",
            f"sqlite:///{sqlite_db}",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            print(f"INFO:  {line}")
    if result.returncode == 0:
        print(render_sqlite_migration_log(event="migrate-ok", sqlite_db=str(sqlite_db)), end="")
        return 0
    print(render_sqlite_migration_log(event="migrate-failed", sqlite_db=str(sqlite_db)), end="")
    print(render_sqlite_migration_log(event="migrate-partial", sqlite_db=str(sqlite_db)), end="")
    return result.returncode or 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ajuste pg_hba.conf pour l'authentification TCP locale TranscrIA.")
    parser.add_argument("path", nargs="?", help="chemin du pg_hba.conf à ajuster")
    parser.add_argument("--dsn", action="store_true", help="affiche le DSN PostgreSQL SQLAlchemy")
    parser.add_argument("--is-local-host", action="store_true", help="teste si --host est local")
    parser.add_argument("--backup-sqlite", action="store_true", help="copie une base SQLite avant migration PostgreSQL")
    parser.add_argument("--run-sqlite-migration", action="store_true", help="sauvegarde puis migre SQLite vers PostgreSQL")
    parser.add_argument("--file-size", action="store_true", help="affiche la taille humaine d'un fichier")
    parser.add_argument("--generate-password", action="store_true", help="génère un mot de passe PostgreSQL")
    parser.add_argument("--validate-inputs", action="store_true", help="valide db/user/port PostgreSQL")
    parser.add_argument("--schema-action", action="store_true", help="décide l'action Alembic depuis has_schema/has_data")
    parser.add_argument("--sqlite-migration-action", action="store_true", help="décide l'action de migration SQLite vers PostgreSQL")
    parser.add_argument("--role-sql", action="store_true", help="rend le SQL idempotent du rôle PostgreSQL")
    parser.add_argument("--database-sql", action="store_true", help="rend le SQL idempotent de création de base PostgreSQL")
    parser.add_argument("--state-query", choices=sorted(_STATE_QUERIES), default=None, help="rend une requête de lecture d'état PostgreSQL")
    parser.add_argument("--encoding-warnings", action="store_true", help="rend les avertissements d'encodage PostgreSQL")
    parser.add_argument("--connection-failure", action="store_true", help="rend les messages d'échec de connexion PostgreSQL")
    parser.add_argument("--state-summary", action="store_true", help="rend le résumé d'état PostgreSQL")
    parser.add_argument("--schema-action-log", action="store_true", help="rend le message initial d'une action Alembic")
    parser.add_argument("--pg-hba-rewrite-result", action="store_true", help="interprète le résultat changed=N de réécriture pg_hba.conf")
    parser.add_argument("--setup-log", action="store_true", help="rend un message de bootstrap PostgreSQL")
    parser.add_argument("--alembic-log", action="store_true", help="rend un message de résultat Alembic PostgreSQL")
    parser.add_argument("--sqlite-migration-log", action="store_true", help="rend un message de migration SQLite vers PostgreSQL")
    parser.add_argument(
        "--sqlite-migration-prompt",
        action="store_true",
        help="rend le prompt interactif de migration SQLite vers PostgreSQL",
    )
    parser.add_argument("--database-setup-log", action="store_true", help="rend un message de choix SQLite/PostgreSQL")
    parser.add_argument("--fallback-locale-c", action="store_true", help="utilise LC_COLLATE/LC_CTYPE C pour --database-sql")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default="5432")
    parser.add_argument("--db", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--sqlite-db", default=None)
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--suffix", default=None)
    parser.add_argument("--has-schema", default=None)
    parser.add_argument("--has-data", default=None)
    parser.add_argument("--sqlite-present", default=None)
    parser.add_argument("--non-interactive", default=None)
    parser.add_argument("--pg-migrate", default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--local-pg", default=None)
    parser.add_argument("--alembic-version", default="")
    parser.add_argument("--action", default=None)
    parser.add_argument("--result", default=None)
    parser.add_argument("--event", default=None)
    parser.add_argument("--sqlite-size", default=None)
    parser.add_argument("--backup-path", default="")
    parser.add_argument("--install-dir", default=None)
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    if args.is_local_host:
        if args.host is None:
            print("--host requis avec --is-local-host", file=sys.stderr)
            return 2
        return 0 if is_local_pg_host(args.host) else 1

    if args.dsn:
        missing = [name for name in ("host", "db", "user", "password") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --dsn: {', '.join(missing)}", file=sys.stderr)
            return 2
        print(build_pg_dsn(args.host, args.port, args.db, args.user, args.password))
        return 0

    if args.backup_sqlite:
        missing = [name for name in ("sqlite_db", "backup_dir", "suffix") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --backup-sqlite: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(backup_sqlite_database(Path(args.sqlite_db), Path(args.backup_dir), args.suffix))
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.run_sqlite_migration:
        missing = [
            name
            for name in ("database_url", "sqlite_db", "backup_dir", "suffix", "install_dir", "python_bin")
            if getattr(args, name) is None
        ]
        if missing:
            print(f"arguments manquants pour --run-sqlite-migration: {', '.join(missing)}", file=sys.stderr)
            return 2
        return run_sqlite_migration(
            dsn=args.database_url,
            sqlite_db=Path(args.sqlite_db),
            backup_dir=Path(args.backup_dir),
            suffix=args.suffix,
            install_dir=Path(args.install_dir),
            python_bin=args.python_bin,
        )

    if args.file_size:
        if args.path is None:
            print("path requis avec --file-size", file=sys.stderr)
            return 2
        try:
            print(human_file_size(Path(args.path)))
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.generate_password:
        print(generate_pg_password())
        return 0

    if args.validate_inputs:
        missing = [name for name in ("db", "user", "port") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --validate-inputs: {', '.join(missing)}", file=sys.stderr)
            return 2
        errors = validate_pg_inputs(args.db, args.user, args.port)
        for error in errors:
            print(error)
        return 1 if errors else 0

    if args.schema_action:
        missing = [name for name in ("has_schema", "has_data") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --schema-action: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(decide_schema_action(args.has_schema, args.has_data))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.sqlite_migration_action:
        missing = [name for name in ("sqlite_present", "has_data", "non_interactive", "pg_migrate") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --sqlite-migration-action: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(
                decide_sqlite_migration_action(
                    sqlite_present=args.sqlite_present,
                    has_data=args.has_data,
                    non_interactive=args.non_interactive,
                    pg_migrate=args.pg_migrate,
                )
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.role_sql:
        print(render_role_sql(), end="")
        return 0

    if args.database_sql:
        print(render_database_sql(fallback_locale_c=args.fallback_locale_c), end="")
        return 0

    if args.state_query is not None:
        print(render_state_query(args.state_query), end="")
        return 0

    if args.encoding_warnings:
        missing = [name for name in ("db", "encoding") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --encoding-warnings: {', '.join(missing)}", file=sys.stderr)
            return 2
        print(render_encoding_warnings(args.db, args.encoding), end="")
        return 0

    if args.connection_failure:
        missing = [name for name in ("db", "user", "host", "port", "local_pg") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --connection-failure: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(render_connection_failure(db=args.db, user=args.user, host=args.host, port=args.port, local_pg=args.local_pg), end="")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.state_summary:
        missing = [name for name in ("db", "has_schema", "has_data") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --state-summary: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(
                render_state_summary(
                    db=args.db,
                    has_schema=args.has_schema,
                    has_data=args.has_data,
                    alembic_version=args.alembic_version,
                ),
                end="",
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.schema_action_log:
        missing = [name for name in ("db", "action") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --schema-action-log: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(render_schema_action_log(db=args.db, action=args.action), end="")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.pg_hba_rewrite_result:
        if args.result is None:
            print("--result requis avec --pg-hba-rewrite-result", file=sys.stderr)
            return 2
        try:
            print(render_pg_hba_rewrite_result(args.result), end="")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.setup_log:
        missing = [name for name in ("event", "db", "user", "host") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --setup-log: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(render_setup_log(event=args.event, db=args.db, user=args.user, host=args.host), end="")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.alembic_log:
        if args.event is None:
            print("--event requis avec --alembic-log", file=sys.stderr)
            return 2
        try:
            print(render_alembic_log(event=args.event, action=args.action or ""), end="")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.sqlite_migration_log:
        missing = [name for name in ("event", "sqlite_db") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --sqlite-migration-log: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            print(
                render_sqlite_migration_log(
                    event=args.event,
                    sqlite_db=args.sqlite_db,
                    action=args.action or "",
                    backup_path=args.backup_path,
                ),
                end="",
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.sqlite_migration_prompt:
        missing = [name for name in ("sqlite_db", "sqlite_size", "db", "host", "port") if getattr(args, name) is None]
        if missing:
            print(f"arguments manquants pour --sqlite-migration-prompt: {', '.join(missing)}", file=sys.stderr)
            return 2
        print(
            render_sqlite_migration_prompt(
                sqlite_db=args.sqlite_db,
                sqlite_size=args.sqlite_size,
                db=args.db,
                host=args.host,
                port=args.port,
            ),
            end="",
        )
        return 0

    if args.database_setup_log:
        if args.event is None:
            print("--event requis avec --database-setup-log", file=sys.stderr)
            return 2
        try:
            print(
                render_database_setup_log(
                    event=args.event,
                    user=args.user or "",
                    db=args.db or "",
                    host=args.host or "",
                    port=args.port,
                ),
                end="",
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.path is None:
        print("path requis hors --dsn/--is-local-host", file=sys.stderr)
        return 2

    try:
        changed = rewrite_pg_hba_file(Path(args.path))
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

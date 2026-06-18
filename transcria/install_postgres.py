from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

_LOCAL_TCP_PEER_RE = re.compile(
    r"^(?P<prefix>\s*host\s+(?:all|replication)\s+all\s+(?:127\.0\.0\.1/32|::1/128)\s+)(?:ident|peer)(?P<suffix>\s*(?:#.*)?)$"
)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ajuste pg_hba.conf pour l'authentification TCP locale TranscrIA.")
    parser.add_argument("path", nargs="?", help="chemin du pg_hba.conf à ajuster")
    parser.add_argument("--dsn", action="store_true", help="affiche le DSN PostgreSQL SQLAlchemy")
    parser.add_argument("--is-local-host", action="store_true", help="teste si --host est local")
    parser.add_argument("--backup-sqlite", action="store_true", help="copie une base SQLite avant migration PostgreSQL")
    parser.add_argument("--file-size", action="store_true", help="affiche la taille humaine d'un fichier")
    parser.add_argument("--generate-password", action="store_true", help="génère un mot de passe PostgreSQL")
    parser.add_argument("--validate-inputs", action="store_true", help="valide db/user/port PostgreSQL")
    parser.add_argument("--schema-action", action="store_true", help="décide l'action Alembic depuis has_schema/has_data")
    parser.add_argument("--sqlite-migration-action", action="store_true", help="décide l'action de migration SQLite vers PostgreSQL")
    parser.add_argument("--role-sql", action="store_true", help="rend le SQL idempotent du rôle PostgreSQL")
    parser.add_argument("--database-sql", action="store_true", help="rend le SQL idempotent de création de base PostgreSQL")
    parser.add_argument("--state-query", choices=sorted(_STATE_QUERIES), default=None, help="rend une requête de lecture d'état PostgreSQL")
    parser.add_argument("--encoding-warnings", action="store_true", help="rend les avertissements d'encodage PostgreSQL")
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

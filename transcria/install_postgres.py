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
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default="5432")
    parser.add_argument("--db", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--sqlite-db", default=None)
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--suffix", default=None)
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

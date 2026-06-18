from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path


def generate_hex_secret(nbytes: int = 32) -> str:
    """Génère un secret hexadécimal adapté à `SECRET_KEY` Flask."""
    return secrets.token_hex(nbytes)


def generate_secret_token(nbytes: int = 32) -> str:
    """Génère un secret URL-safe adapté aux clés API internes."""
    return secrets.token_urlsafe(nbytes)


def _active_env_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def get_env_value(path: Path, key: str) -> str | None:
    """Lit une variable active depuis un fichier `.env`."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return _active_env_value(lines, key)


def has_any_env_key(path: Path, keys: list[str]) -> bool:
    """Retourne vrai si au moins une clé active existe dans le fichier `.env`."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return any(_active_env_value(lines, key) is not None for key in keys)


def set_env_value(lines: list[str], key: str, value: str, *, comment: str | None = None) -> list[str]:
    """Retourne des lignes `.env` avec `key=value`, en préservant le reste.

    Les lignes commentées de la forme `# KEY=...` sont remplacées par la valeur
    active. Si plusieurs occurrences existent, la première est remplacée et les
    suivantes sont conservées telles quelles pour ne pas masquer une ambiguïté.
    """
    updated: list[str] = []
    done = False
    prefix = f"{key}="
    for line in lines:
        stripped = line.lstrip()
        uncommented = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        if not done and uncommented.startswith(prefix):
            updated.append(f"{key}={value}")
            done = True
        else:
            updated.append(line)
    if not done:
        if updated and updated[-1] != "":
            updated.append("")
        if comment:
            updated.append(f"# {comment}")
        updated.append(f"{key}={value}")
    return updated


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Écrit un fichier texte atomiquement dans le même répertoire."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def update_env_file(path: Path, key: str, value: str, *, backup: bool = True, comment: str | None = None) -> Path | None:
    """Met à jour une variable `.env` atomiquement et retourne le backup créé."""
    path = Path(path)
    original = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    backup_path: Path | None = None
    if backup and path.exists():
        backup_path = path.with_name(f"{path.name}.bak")
        atomic_write_text(backup_path, path.read_text(encoding="utf-8"), mode=0o600)
    updated = set_env_value(original, key, value, comment=comment)
    atomic_write_text(path, "\n".join(updated) + "\n", mode=0o600)
    return backup_path


def init_env_file_from_template(path: Path, template_path: Path) -> str:
    """Crée `.env` depuis un template si absent et retourne `created|present`."""
    path = Path(path)
    template_path = Path(template_path)
    if path.exists():
        return "present"
    content = template_path.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        content += "\n"
    atomic_write_text(path, content, mode=0o600)
    return "created"


def ensure_env_secret(
    path: Path,
    key: str,
    *,
    min_length: int,
    placeholder: str | None = None,
    generator: str = "urlsafe",
    comment: str | None = None,
) -> str:
    """Garantit qu'une variable secrète active existe et retourne `present|created`."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    current = _active_env_value(lines, key)
    if current and len(current) >= min_length and (placeholder is None or current != placeholder):
        return "present"
    if generator == "hex":
        value = generate_hex_secret()
    elif generator == "urlsafe":
        value = generate_secret_token()
    else:
        raise ValueError(f"generator inconnu: {generator}")
    update_env_file(path, key, value, backup=False, comment=comment)
    return "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manipulation atomique de fichiers .env TranscrIA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="définit KEY=VALUE dans un fichier .env")
    set_parser.add_argument("--env-file", required=True)
    set_parser.add_argument("--key", required=True)
    set_parser.add_argument("--value", required=True)
    set_parser.add_argument("--backup", action="store_true", help="crée un backup .env.bak avant écriture")
    set_parser.add_argument("--comment", default=None, help="commentaire ajouté si la clé est absente")

    get_parser = subparsers.add_parser("get", help="lit KEY depuis un fichier .env")
    get_parser.add_argument("--env-file", required=True)
    get_parser.add_argument("--key", required=True)

    has_any_parser = subparsers.add_parser("has-any", help="teste si au moins une clé active existe")
    has_any_parser.add_argument("--env-file", required=True)
    has_any_parser.add_argument("--key", action="append", required=True)

    init_parser = subparsers.add_parser("init", help="crée .env depuis un template si absent")
    init_parser.add_argument("--env-file", required=True)
    init_parser.add_argument("--template", required=True)

    secret_parser = subparsers.add_parser("ensure-secret", help="génère une clé secrète si absente ou invalide")
    secret_parser.add_argument("--env-file", required=True)
    secret_parser.add_argument("--key", required=True)
    secret_parser.add_argument("--min-length", type=int, required=True)
    secret_parser.add_argument("--placeholder", default=None)
    secret_parser.add_argument("--generator", choices=("hex", "urlsafe"), default="urlsafe")
    secret_parser.add_argument("--comment", default=None, help="commentaire ajouté si la clé est absente")

    args = parser.parse_args(argv)
    if args.command == "get":
        value = get_env_value(Path(args.env_file), args.key)
        if value is not None:
            print(value)
        return 0
    if args.command == "has-any":
        return 0 if has_any_env_key(Path(args.env_file), args.key) else 1
    if args.command == "set":
        update_env_file(Path(args.env_file), args.key, args.value, backup=args.backup, comment=args.comment)
        return 0
    if args.command == "init":
        print(init_env_file_from_template(Path(args.env_file), Path(args.template)))
        return 0
    if args.command == "ensure-secret":
        status = ensure_env_secret(
            Path(args.env_file),
            args.key,
            min_length=args.min_length,
            placeholder=args.placeholder,
            generator=args.generator,
            comment=args.comment,
        )
        print(status)
        return 0
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

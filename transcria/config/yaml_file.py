from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


def _split_key_path(key: str) -> list[str]:
    parts = [part for part in key.split(".") if part]
    if not parts:
        raise ValueError("clé YAML vide")
    return parts


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} ne contient pas un mapping YAML")
    return data


def get_yaml_value(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in _split_key_path(key):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_yaml_value(data: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    node: dict[str, Any] = data
    parts = _split_key_path(key)
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"impossible d'écrire {key}: {part} n'est pas un mapping")
        node = child
    node[parts[-1]] = value
    return data


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, content: str) -> None:
    """Écrit un texte atomiquement dans le même répertoire."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def backup_yaml_file(path: Path, suffix: str) -> Path:
    """Sauvegarde un fichier YAML existant et retourne le chemin du backup."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    backup_path = path.with_name(f"{path.name}.bak.{suffix}")
    atomic_write_text(backup_path, path.read_text(encoding="utf-8"))
    return backup_path


def set_yaml_file_value(path: Path, key: str, value: str) -> None:
    data = load_yaml_file(path)
    set_yaml_value(data, key, value)
    atomic_write_yaml(path, data)


def _format_cli_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lecture/écriture atomique de valeurs YAML TranscrIA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="lit une valeur par chemin pointé")
    get_parser.add_argument("--file", required=True)
    get_parser.add_argument("--key", required=True)

    set_parser = subparsers.add_parser("set", help="définit une valeur chaîne par chemin pointé")
    set_parser.add_argument("--file", required=True)
    set_parser.add_argument("--key", required=True)
    set_parser.add_argument("--value", required=True)

    backup_parser = subparsers.add_parser("backup", help="crée une copie de secours atomique")
    backup_parser.add_argument("--file", required=True)
    backup_parser.add_argument("--suffix", required=True)

    args = parser.parse_args(argv)
    try:
        path = Path(args.file)
        if args.command == "get":
            print(_format_cli_value(get_yaml_value(load_yaml_file(path), args.key)))
            return 0
        if args.command == "set":
            set_yaml_file_value(path, args.key, args.value)
            return 0
        if args.command == "backup":
            print(backup_yaml_file(path, args.suffix))
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

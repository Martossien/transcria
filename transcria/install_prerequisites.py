"""Vérifications de prérequis système pour l'installateur."""
from __future__ import annotations

import argparse
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

WhichFn = Callable[[str], str | None]


@dataclass(frozen=True)
class BinaryCheck:
    name: str
    path: Path | None
    required: bool

    @property
    def found(self) -> bool:
        return self.path is not None

    @property
    def status(self) -> str:
        if self.found:
            return "OK"
        if self.required:
            return "MISSING_REQUIRED"
        return "MISSING_OPTIONAL"


def check_binaries(required: list[str], optional: list[str] | None = None, *, which: WhichFn = shutil.which) -> list[BinaryCheck]:
    """Retourne l'état des binaires requis et optionnels, dans l'ordre demandé."""
    checks: list[BinaryCheck] = []
    seen: set[str] = set()
    for required_flag, names in ((True, required), (False, optional or [])):
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            found = which(name)
            checks.append(BinaryCheck(name=name, path=Path(found) if found else None, required=required_flag))
    return checks


def render_binary_checks(checks: list[BinaryCheck]) -> str:
    """Rend les checks sous forme TSV stable pour `install.sh`."""
    lines = []
    for check in checks:
        lines.append(f"{check.status}\t{check.name}\t{check.path or ''}")
    return "\n".join(lines)


def has_missing_required(checks: list[BinaryCheck]) -> bool:
    return any(check.required and not check.found for check in checks)


def first_available(names: list[str], *, which: WhichFn = shutil.which) -> BinaryCheck | None:
    """Retourne le premier binaire disponible selon l'ordre de préférence demandé."""
    return next((check for check in check_binaries(names, which=which) if check.found), None)


def render_first_available(check: BinaryCheck, *, output_format: str) -> str:
    if check.path is None:
        raise ValueError("un binaire disponible doit avoir un chemin")
    if output_format == "path":
        return str(check.path)
    if output_format == "name":
        return check.name
    if output_format == "shell":
        return "\n".join(
            [
                f"FIRST_AVAILABLE_NAME={shlex.quote(check.name)}",
                f"FIRST_AVAILABLE_PATH={shlex.quote(str(check.path))}",
            ]
        )
    if output_format == "tsv":
        return f"{check.name}\t{check.path}"
    raise ValueError(f"format non supporté: {output_format}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers de prérequis système TranscrIA.")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check-binaries", help="vérifie la présence de binaires système")
    check_parser.add_argument("--required", action="append", default=[])
    check_parser.add_argument("--optional", action="append", default=[])

    first_parser = subparsers.add_parser("first-available", help="retourne le premier binaire disponible")
    first_parser.add_argument("--name", action="append", required=True)
    first_parser.add_argument("--format", choices=["tsv", "shell", "path", "name"], default="tsv")

    args = parser.parse_args(argv)
    if args.command == "check-binaries":
        checks = check_binaries(args.required, args.optional)
        output = render_binary_checks(checks)
        if output:
            print(output)
        return 1 if has_missing_required(checks) else 0
    if args.command == "first-available":
        match = first_available(args.name)
        if match is None:
            return 1
        print(render_first_available(match, output_format=args.format))
        return 0

    print("commande prérequis inconnue", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

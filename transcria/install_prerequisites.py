"""Vérifications de prérequis système pour l'installateur."""
from __future__ import annotations

import argparse
import importlib.util
import pwd
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

WhichFn = Callable[[str], str | None]
UserHomeFn = Callable[[str], str]
FindSpecFn = Callable[[str], object | None]


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


def python_venv_supported(*, find_spec: FindSpecFn = importlib.util.find_spec) -> bool:
    """Vrai si l'interpréteur courant peut créer un venv AVEC pip.

    Sur Debian/Ubuntu, `python3` est présent mais le paquet `python3-venv` (qui fournit
    `ensurepip`) est séparé : `python -m venv` échoue alors avec un message obscur
    (« ensurepip is not available »). On vérifie en amont pour émettre un message clair
    et stopper l'installation AVANT le plantage. Vérifié sur l'interpréteur qui exécute
    ce module — c'est exactement celui qui créera le venv (PYTHON_BIN dans install.sh).
    """
    try:
        return find_spec("venv") is not None and find_spec("ensurepip") is not None
    except (ImportError, ValueError):
        return False


def detect_system_capabilities(*, which: WhichFn = shutil.which) -> dict[str, bool]:
    """Détecte les outils système utilisés par les branches privilégiées de l'installateur."""
    binaries = {
        "sudo": "HAVE_SUDO",
        "runuser": "HAVE_RUNUSER",
        "systemctl": "HAVE_SYSTEMCTL",
        "service": "HAVE_SERVICE",
        "nvidia-smi": "HAVE_NVIDIA_SMI",
    }
    return {variable: which(binary) is not None for binary, variable in binaries.items()}


def render_system_capabilities(capabilities: dict[str, bool], *, output_format: str) -> str:
    if output_format == "shell":
        return "\n".join(f"{key}={'true' if value else 'false'}" for key, value in sorted(capabilities.items()))
    if output_format == "tsv":
        return "\n".join(f"{key}\t{'1' if value else '0'}" for key, value in sorted(capabilities.items()))
    raise ValueError(f"format non supporté: {output_format}")


def resolve_user_home(user: str, *, get_home: UserHomeFn | None = None) -> str:
    """Retourne le home d'un utilisateur système."""
    if get_home is not None:
        return get_home(user)
    return pwd.getpwnam(user).pw_dir


def render_setup_log(*, event: str, name: str = "", value: str = "", path: str = "") -> str:
    """Rend les messages de prérequis utilisés par install.sh."""
    if event == "python-ok":
        return f"OK:Python {value} : {path}\n"
    if event == "python-missing":
        return "ERROR:Python 3.11+ requis. Installer avec: apt install python3.11\n"
    if event == "venv-missing":
        return (
            "ERROR:module venv/ensurepip indisponible — `python -m venv` échouerait. "
            "Installer avec: apt install python3-venv\n"
        )
    if event == "nvidia-ok":
        return f"OK:nvidia-smi — {value} GPU(s), CUDA {path}\n"
    if event == "nvidia-missing":
        return "WARN:nvidia-smi non trouvé ou inutilisable — fonctionnement sans GPU (transcription très lente)\n"
    if event == "binary-ok":
        return f"OK:{name} : {path}\n"
    if event == "binary-required-missing":
        if name in {"ffmpeg", "ffprobe"}:
            return f"ERROR:{name} manquant. Installer avec: apt install ffmpeg\n"
        return f"ERROR:{name} manquant.\n"
    if event == "binary-optional-missing":
        if name == "lsof":
            return "WARN:lsof manquant — requis par start.sh/stop.sh. Installer: apt install lsof\n"
        if name == "curl":
            return "WARN:curl manquant — requis pour télécharger opencode (LLM d'arbitrage). Installer: apt install curl\n"
        return f"WARN:{name} manquant\n"
    raise ValueError(f"événement prérequis inconnu : {event}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers de prérequis système TranscrIA.")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check-binaries", help="vérifie la présence de binaires système")
    check_parser.add_argument("--required", action="append", default=[])
    check_parser.add_argument("--optional", action="append", default=[])

    first_parser = subparsers.add_parser("first-available", help="retourne le premier binaire disponible")
    first_parser.add_argument("--name", action="append", required=True)
    first_parser.add_argument("--format", choices=["tsv", "shell", "path", "name"], default="tsv")

    subparsers.add_parser("check-venv", help="vérifie que l'interpréteur peut créer un venv avec pip (ensurepip)")

    caps_parser = subparsers.add_parser("system-capabilities", help="détecte les outils système disponibles")
    caps_parser.add_argument("--format", choices=["tsv", "shell"], default="tsv")

    home_parser = subparsers.add_parser("user-home", help="affiche le home d'un utilisateur système")
    home_parser.add_argument("--user", required=True)

    setup_parser = subparsers.add_parser("setup-log", help="rend un message de prérequis install.sh")
    setup_parser.add_argument("--event", required=True)
    setup_parser.add_argument("--name", default="")
    setup_parser.add_argument("--value", default="")
    setup_parser.add_argument("--path", default="")

    args = parser.parse_args(argv)
    if args.command == "check-binaries":
        checks = check_binaries(args.required, args.optional)
        output = render_binary_checks(checks)
        if output:
            print(output)
        return 1 if has_missing_required(checks) else 0
    if args.command == "check-venv":
        return 0 if python_venv_supported() else 1
    if args.command == "first-available":
        match = first_available(args.name)
        if match is None:
            return 1
        print(render_first_available(match, output_format=args.format))
        return 0
    if args.command == "system-capabilities":
        print(render_system_capabilities(detect_system_capabilities(), output_format=args.format))
        return 0
    if args.command == "user-home":
        try:
            print(resolve_user_home(args.user))
        except KeyError:
            print(f"utilisateur introuvable: {args.user}", file=sys.stderr)
            return 1
        return 0
    if args.command == "setup-log":
        try:
            print(render_setup_log(event=args.event, name=args.name, value=args.value, path=args.path), end="")
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    print("commande prérequis inconnue", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

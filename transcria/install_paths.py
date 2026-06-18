from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirectorySpec:
    """Répertoire attendu par un profil d'installation."""

    path: Path
    label: str


def runtime_directory_specs(install_dir: Path) -> list[DirectorySpec]:
    """Retourne les répertoires runtime communs à préparer pendant l'installation."""
    install_dir = Path(install_dir)
    return [
        DirectorySpec(install_dir / "jobs", "jobs"),
        DirectorySpec(install_dir / "models" / "cohere-asr", "models/cohere-asr"),
        DirectorySpec(install_dir / "instance", "instance"),
    ]


def legacy_service_directory_specs(install_dir: Path) -> list[DirectorySpec]:
    """Retourne les répertoires locaux nécessaires au service legacy non-root."""
    install_dir = Path(install_dir)
    return [
        DirectorySpec(install_dir / "logs", "logs"),
        DirectorySpec(install_dir / "run", "run"),
    ]


def inference_service_directory_specs(install_dir: Path) -> list[DirectorySpec]:
    """Retourne les répertoires locaux nécessaires au service inference non-root."""
    install_dir = Path(install_dir)
    return [
        DirectorySpec(install_dir / "logs", "logs"),
    ]


def ensure_directories(specs: list[DirectorySpec]) -> list[Path]:
    """Crée les répertoires demandés et retourne leurs chemins."""
    created: list[Path] = []
    for spec in specs:
        spec.path.mkdir(parents=True, exist_ok=True)
        created.append(spec.path)
    return created


def ensure_runtime_directories(install_dir: Path) -> list[Path]:
    """Prépare les répertoires runtime communs."""
    return ensure_directories(runtime_directory_specs(install_dir))


def explicit_directory_specs(paths: list[Path]) -> list[DirectorySpec]:
    """Retourne des spécifications pour des chemins explicitement fournis."""
    return [DirectorySpec(Path(path), str(path)) for path in paths]


def directory_specs_for_kind(kind: str, install_dir: Path) -> list[DirectorySpec]:
    """Retourne les répertoires à préparer pour un type de besoin."""
    if kind == "runtime":
        return runtime_directory_specs(install_dir)
    if kind == "legacy-service":
        return legacy_service_directory_specs(install_dir)
    if kind == "inference-service":
        return inference_service_directory_specs(install_dir)
    raise ValueError(f"kind inconnu: {kind}")


def render_setup_log(*, event: str, value: str = "") -> str:
    """Rend les messages de préparation locale des chemins et dépendances."""
    if event == "venv-existing":
        return f"OK:Venv existant : {value}\n"
    if event == "venv-create-start":
        return "INFO:Création du venv...\n"
    if event == "venv-created":
        return f"OK:Venv créé : {value}\n"
    if event == "pip-upgrade":
        return "INFO:Mise à jour de pip...\n"
    if event == "requirements-start":
        return "INFO:Installation requirements.txt...\n"
    if event == "requirements-ok":
        return "OK:requirements.txt installé\n"
    if event == "runtime-dirs-ready":
        return "OK:jobs/, models/, instance/ prêts\n"
    raise ValueError(f"événement chemins inconnu : {event}")


def _print_shell_paths(paths: list[Path]) -> None:
    for index, path in enumerate(paths):
        print(f"INSTALL_PATH_{index}={path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prépare les répertoires runtime TranscrIA.")
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--kind", choices=("runtime", "legacy-service", "inference-service"), default="runtime")
    parser.add_argument("--path", action="append", default=[], help="répertoire explicite à créer ; peut être répété")
    parser.add_argument("--format", choices=("text", "shell"), default="text")
    parser.add_argument("--setup-log", action="store_true", help="rend un message de préparation locale")
    parser.add_argument("--event", default="")
    parser.add_argument("--value", default="")
    args = parser.parse_args(argv)

    if args.setup_log:
        if not args.event:
            print("--event requis avec --setup-log", file=sys.stderr)
            return 2
        try:
            print(render_setup_log(event=args.event, value=args.value), end="")
            return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        if args.path:
            specs = explicit_directory_specs([Path(path) for path in args.path])
        else:
            specs = directory_specs_for_kind(args.kind, Path(args.install_dir))
        paths = ensure_directories(specs)
    except OSError as exc:
        print(f"création des répertoires impossible: {exc}", file=sys.stderr)
        return 2

    if args.format == "shell":
        _print_shell_paths(paths)
    else:
        for path in paths:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

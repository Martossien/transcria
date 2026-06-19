"""Point d'entrée de l'installateur Python : `python -m transcria.installer.cli`.

`install.sh` délègue les phases migrées à ce CLI et n'a plus qu'à vérifier le code
de sortie. Les nouvelles phases s'ajoutent ici en sous-commandes ; l'orchestration
métier vit dans les modules dédiés (testés), pas dans le shell.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from transcria.installer.console import Console
from transcria.installer.python_env import PythonEnvError, PythonEnvPlan, apply_python_env


def _add_python_env_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "python-env",
        help="Provisionne le venv, PyTorch et les dépendances (SECTION 2-4 de install.sh).",
    )
    p.add_argument("--venv", required=True, help="Répertoire du venv (créé si absent)")
    p.add_argument("--requirements", required=True, help="Chemin de requirements.txt")
    p.add_argument("--skip-deps", action="store_true", help="Venv/dépendances déjà fournis : ne rien installer")
    p.add_argument("--no-torch", action="store_true", help="Ne pas installer PyTorch")
    p.add_argument("--cuda-version", default=None, help="Version CUDA détectée (ex. 12.4)")
    p.add_argument("--force-cuda", default=None, help="Forcer le tag wheel (cu121/cu124/cu126/cpu)")


def _cmd_python_env(args: argparse.Namespace) -> int:
    console = Console()
    plan = PythonEnvPlan(
        venv_path=Path(args.venv),
        requirements_path=Path(args.requirements),
        skip_deps=args.skip_deps,
        install_torch=not args.no_torch,
        cuda_version=args.cuda_version,
        forced_cuda_tag=args.force_cuda,
    )
    try:
        apply_python_env(plan, console=console)
    except PythonEnvError as exc:
        console.error(str(exc))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcria-install", description="Installateur TranscrIA piloté en Python.")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_python_env_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "python-env":
        return _cmd_python_env(args)
    parser.error(f"commande inconnue : {args.command}")  # pragma: no cover - argparse garde l'exhaustivité
    return 2


if __name__ == "__main__":
    sys.exit(main())

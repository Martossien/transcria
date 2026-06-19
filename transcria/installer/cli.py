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


def _add_config_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "config",
        help="Génère config.yaml/.env + secrets + rôle runtime (cœur de SECTION 6).",
    )
    p.add_argument("--install-dir", required=True)
    p.add_argument("--config", required=True, help="Chemin de config.yaml")
    p.add_argument("--env-file", required=True)
    p.add_argument("--example-config", required=True, help="Chemin de config.example.yaml")
    p.add_argument("--env-template", required=True, help="Chemin de .env.example")
    p.add_argument("--profile", required=True)
    p.add_argument("--runtime-role", default="")
    p.add_argument("--profile-explicit", action="store_true")
    p.add_argument("--install-inference", action="store_true")
    p.add_argument("--force-config", action="store_true")


def _cmd_config(args: argparse.Namespace) -> int:
    # Import différé : cette phase importe PyYAML (config.yaml_file) et n'est lancée
    # que sous le python du venv ; la phase python-env (pré-venv) ne doit pas la charger.
    from transcria.installer.config_phase import ConfigPlan, apply_config

    console = Console()
    plan = ConfigPlan(
        install_dir=Path(args.install_dir),
        config_path=Path(args.config),
        env_file=Path(args.env_file),
        example_config=Path(args.example_config),
        env_template=Path(args.env_template),
        profile=args.profile,
        runtime_role=args.runtime_role,
        profile_explicit=args.profile_explicit,
        install_inference=args.install_inference,
        force_config=args.force_config,
    )
    apply_config(plan, console=console)
    return 0


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
    _add_config_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "python-env":
        return _cmd_python_env(args)
    if args.command == "config":
        return _cmd_config(args)
    parser.error(f"commande inconnue : {args.command}")  # pragma: no cover - argparse garde l'exhaustivité
    return 2


if __name__ == "__main__":
    sys.exit(main())

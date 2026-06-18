#!/usr/bin/env python3
"""Rotation de la clé API du nœud de ressources GPU.

Par défaut, le script génère une nouvelle valeur pour `TRANSCRIA_INFERENCE_API_KEY`
dans `.env`, crée un backup `.env.bak`, et n'affiche pas le secret. Utiliser
`--print-key` seulement dans un terminal sûr.

Exemples :
    venv/bin/python scripts/rotate_resource_node_key.py
    venv/bin/python scripts/rotate_resource_node_key.py --env-file /etc/transcria/resource.env
    venv/bin/python scripts/rotate_resource_node_key.py --value "$NEW_KEY" --print-key
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from transcria.config.env_file import generate_secret_token, update_env_file  # noqa: E402

DEFAULT_KEY = "TRANSCRIA_INFERENCE_API_KEY"


def rotate_key(
    env_file: Path,
    *,
    key_name: str = DEFAULT_KEY,
    value: str | None = None,
    backup: bool = True,
    dry_run: bool = False,
) -> tuple[str, Path | None]:
    """Génère ou applique une clé et met à jour `.env` sauf en dry-run."""
    new_value = value or generate_secret_token()
    if dry_run:
        return new_value, None
    backup_path = update_env_file(env_file, key_name, new_value, backup=backup)
    return new_value, backup_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="fichier .env à modifier")
    parser.add_argument("--key-name", default=DEFAULT_KEY, help="nom de variable à mettre à jour")
    parser.add_argument("--value", default=None, help="valeur imposée au lieu d'une génération aléatoire")
    parser.add_argument("--no-backup", action="store_true", help="ne pas créer de backup .env.bak")
    parser.add_argument("--dry-run", action="store_true", help="ne rien écrire, valider seulement les paramètres")
    parser.add_argument("--print-key", action="store_true", help="afficher la nouvelle clé en clair")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file)
    if args.value is not None and len(args.value) < 16:
        print("[FAIL] --value doit contenir au moins 16 caractères", file=sys.stderr)
        return 2

    new_value, backup_path = rotate_key(
        env_file,
        key_name=args.key_name,
        value=args.value,
        backup=not args.no_backup,
        dry_run=args.dry_run,
    )

    action = "dry-run" if args.dry_run else "rotation"
    print(f"[OK] {action} {args.key_name} dans {env_file}")
    if backup_path:
        print(f"[OK] backup : {backup_path}")
    if args.print_key:
        print(f"{args.key_name}={new_value}")
    else:
        print("[INFO] clé non affichée ; utilisez --print-key seulement dans un terminal sûr")
    if not args.dry_run:
        print("[NEXT] redémarrer le service : sudo systemctl restart transcria-inference")
        print("[NEXT] lancer le smoke : venv/bin/python scripts/smoke_resource_node.py --api-key-env " + args.key_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

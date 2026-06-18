from __future__ import annotations

import argparse
import sys


def parse_non_negative_int(value: str) -> int:
    """Parse un entier CLI non négatif pour les appels depuis install.sh."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"entier invalide: {value}") from exc
    if parsed < 0:
        raise ValueError(f"entier négatif invalide: {value}")
    return parsed


def render_database_summary(db_backend: str) -> str:
    """Rend le bilan final de base de données."""
    lines = ["Base de données :"]
    if db_backend.startswith("PostgreSQL"):
        lines.append(f"  [OK] {db_backend} — DSN dans .env (TRANSCRIA_DATABASE_URL)")
    else:
        lines.append(f"  [INFO] {db_backend} — réservé au dev local ; passez à PostgreSQL hors dev : ./install.sh --postgres")
    return "\n".join(lines) + "\n"


def render_configuration_summary(*, config_path: str, remaining_changes: int, doctor_status: str) -> str:
    """Rend le bilan final de configuration."""
    lines = ["Configuration :"]
    if remaining_changes > 0:
        lines.append(f"  [WARN] {config_path} contient encore {remaining_changes} valeur(s) 'CHANGE-ME'")
        lines.append("         Éditer config.yaml avant le premier démarrage")
    else:
        lines.append("  [OK] config.yaml — aucune valeur par défaut restante")
    lines.append(f"  [INFO] doctor.py : {doctor_status}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rendu du résumé final d'installation TranscrIA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("database", help="rend le bilan base de données")
    db_parser.add_argument("--db-backend", required=True)

    config_parser = subparsers.add_parser("configuration", help="rend le bilan configuration")
    config_parser.add_argument("--config-path", required=True)
    config_parser.add_argument("--remaining-changes", required=True)
    config_parser.add_argument("--doctor-status", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "database":
            print(render_database_summary(args.db_backend), end="")
            return 0
        if args.command == "configuration":
            print(
                render_configuration_summary(
                    config_path=args.config_path,
                    remaining_changes=parse_non_negative_int(args.remaining_changes),
                    doctor_status=args.doctor_status,
                ),
                end="",
            )
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

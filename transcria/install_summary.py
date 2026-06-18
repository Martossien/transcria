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


def render_setup_log(*, event: str, profile: str = "", runtime_role: str = "", value: str = "") -> str:
    """Rend les messages de la section configuration de install.sh."""
    if event == "config-kept":
        return "OK:config.yaml existant conservé\n"
    if event == "force-hint":
        return "INFO:(--force-config pour régénérer)\n"
    if event == "config-backup":
        return f"INFO:Ancien config.yaml sauvegardé : {value}\n"
    if event == "config-generate-start":
        return "INFO:Génération via bootstrap_config.py (auto-détection)...\n"
    if event == "config-generated":
        return "OK:config.yaml généré\n"
    if event == "secret-created":
        return "OK:Clé secrète Flask générée dans .env\n"
    if event == "secret-present":
        return "OK:TRANSCRIA_SECRET présent dans .env\n"
    if event == "profile-runtime":
        return f"OK:Profil d'installation : {profile} (TRANSCRIA_ROLE={runtime_role})\n"
    if event == "profile-all-default":
        return "OK:Profil d'installation : all-in-one (défaut)\n"
    if event == "profile-resource-node":
        return "OK:Profil d'installation : resource-node (inference_service)\n"
    if event == "profile-migrate":
        return "OK:Profil d'installation : migrate (Alembic only)\n"
    if event == "profile-generic":
        return f"OK:Profil d'installation : {profile}\n"
    if event == "inference-key-present":
        return "OK:TRANSCRIA_INFERENCE_API_KEY présent dans .env\n"
    if event == "inference-key-created":
        return "OK:TRANSCRIA_INFERENCE_API_KEY généré dans .env (chmod 600)\n"
    if event == "proxy-present":
        return "OK:Proxy déjà présent dans .env\n"
    if event == "proxy-persisted":
        return "OK:Proxy persisté dans .env (http_proxy/https_proxy/no_proxy)\n"
    raise ValueError(f"événement de configuration inconnu : {event}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rendu du résumé final d'installation TranscrIA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("database", help="rend le bilan base de données")
    db_parser.add_argument("--db-backend", required=True)

    config_parser = subparsers.add_parser("configuration", help="rend le bilan configuration")
    config_parser.add_argument("--config-path", required=True)
    config_parser.add_argument("--remaining-changes", required=True)
    config_parser.add_argument("--doctor-status", required=True)

    setup_parser = subparsers.add_parser("setup-log", help="rend un message de configuration install.sh")
    setup_parser.add_argument("--event", required=True)
    setup_parser.add_argument("--profile", default="")
    setup_parser.add_argument("--runtime-role", default="")
    setup_parser.add_argument("--value", default="")

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
        if args.command == "setup-log":
            print(render_setup_log(event=args.event, profile=args.profile, runtime_role=args.runtime_role, value=args.value), end="")
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"commande inconnue: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

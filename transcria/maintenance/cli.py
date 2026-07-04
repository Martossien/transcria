"""CLI de maintenance opérateur — ``python -m transcria.maintenance.cli`` (C1.1/C1.2).

Sous-commandes :
    backup            crée une archive tar.gz horodatée (+ rotation, +manifeste)
    backup-verify     vérifie l'intégrité d'une archive (sha256 + ouverture réelle)
    restore           restaure une archive (garde-fous : base vide sauf --force, dry-run)

La logique métier est dans backup.py / restore.py (pur, testé) ; ce module est le runner.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_cfg_and_meta(config_path: str | None):
    from transcria import __version__
    from transcria.config.loader import get_config_path, load_config

    resolved = get_config_path(config_path)
    cfg = load_config(config_path)
    revision = _current_alembic_revision(cfg)
    return cfg, resolved, __version__, revision


def _current_alembic_revision(cfg: dict) -> str | None:
    try:
        from alembic.migration import MigrationContext
        from sqlalchemy import create_engine

        engine = create_engine(str(cfg["storage"]["database_url"]))
        try:
            with engine.connect() as conn:
                return MigrationContext.configure(conn).get_current_revision()
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 — la révision est informative, jamais bloquante
        return None


def _cmd_backup(args: argparse.Namespace) -> int:
    from transcria.maintenance.backup import create_backup, rotate_backups

    cfg, resolved, version, revision = _load_cfg_and_meta(args.config)
    env_path = Path(args.env) if args.env else Path(".env")
    dest = Path(args.dest)
    archive = create_backup(
        cfg, resolved, dest,
        app_version=version,
        alembic_revision=revision,
        include_audio=not args.exclude_audio,
        env_path=env_path if env_path.exists() else None,
    )
    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"✅ Sauvegarde créée : {archive}  ({size_mb:.1f} Mo)")
    if args.keep:
        removed = rotate_backups(dest, args.keep)
        if removed:
            print(f"   Rotation : {len(removed)} ancienne(s) archive(s) supprimée(s).")
    print("   Vérifiez-la : python -m transcria.maintenance.cli backup-verify " + str(archive))
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    from transcria.maintenance.backup import read_manifest, verify_backup

    archive = Path(args.archive)
    problems = verify_backup(archive)
    if problems:
        print("❌ Archive INVALIDE :", file=sys.stderr)
        for p in problems:
            print(f"   - {p}", file=sys.stderr)
        return 1
    manifest = read_manifest(archive)
    print(f"✅ Archive saine : {archive.name}")
    print(f"   Créée le {manifest.get('created_at')} · version {manifest.get('app_version')} "
          f"· base {manifest.get('db_kind')} · révision {manifest.get('alembic_revision')}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from transcria.maintenance.backup import BackupError
    from transcria.maintenance.restore import describe_restore, restore_backup

    cfg, resolved, _version, _revision = _load_cfg_and_meta(args.config)
    cfg["_config_path"] = resolved
    archive = Path(args.archive)

    if args.dry_run:
        info = describe_restore(archive)
        print("— Restauration À BLANC (rien n'est écrit) —")
        for key, value in info.items():
            print(f"   {key}: {value}")
        print("   Relancez sans --dry-run pour appliquer.")
        return 0

    try:
        report = restore_backup(cfg, archive, force=args.force, ready_url=args.ready_url)
    except BackupError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"✅ Restauration terminée depuis {report['restored_from']}")
    if report.get("config_restored_as"):
        print(f"   ⚠ config de l'archive déposée en {report['config_restored_as']} — "
              "comparez-la à votre config.yaml (jamais écrasé automatiquement).")
    print(f"   version {report['app_version']} · base {report['db_kind']} "
          f"· révision attendue {report['alembic_revision']}")
    print("   Vérifiez l'alignement du schéma : venv/bin/python scripts/doctor.py")
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> int:
    from pathlib import Path

    from transcria.maintenance.backup import create_backup, rotate_backups
    from transcria.maintenance.upgrade import (
        UpgradeError,
        build_plan,
        changelog_excerpt,
        default_ready_check,
        run_plan,
    )

    cfg, resolved, version, revision = _load_cfg_and_meta(args.config)
    units = [u for u in (args.units or "transcria.service").split(",") if u]
    steps = build_plan(target_ref=args.ref, do_pull=not args.ref,
                       restart_units=units, ready_url=args.ready_url)

    if args.check:
        print("— Mise à niveau À BLANC (aucune action) —")
        for i, step in enumerate(steps, 1):
            detail = " ".join(step.command) if step.command else f"[{step.internal}]"
            print(f"   {i}. {step.label}  →  {detail}")
        print("   Relancez sans --check pour appliquer.")
        return 0

    dest = Path(args.backup_dest)

    def _backup():
        archive = create_backup(cfg, resolved, dest, app_version=version,
                                alembic_revision=revision,
                                env_path=Path(".env") if Path(".env").exists() else None)
        rotate_backups(dest, args.keep) if args.keep else None
        return archive

    try:
        run_plan(steps, backup_fn=_backup,
                 healthcheck_fn=lambda: default_ready_check(args.ready_url))
    except UpgradeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print("✅ Mise à niveau terminée.")
    whats_new = changelog_excerpt(Path("CHANGELOG.md"))
    if whats_new:
        print("\n— Quoi de neuf —\n" + whats_new)
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    """Purge des données expirées selon la politique de rétention (C3.10).
    ``--dry-run`` COMPTE sans rien supprimer."""
    from transcria.config.loader import load_config

    cfg = load_config(args.config)
    from app import create_app

    app = create_app(args.config)
    with app.app_context():
        from transcria.audit.store import AuditStore
        from transcria.jobs.store import JobStore

        sec = cfg.get("security", {})
        jobs_dir = cfg["storage"]["jobs_dir"]
        job_count = JobStore.purge_expired_jobs(sec.get("retention_days"), jobs_dir, dry_run=args.dry_run)
        mode = "à purger (simulation)" if args.dry_run else "purgés"
        print(f"Traitements expirés {mode} (rétention {sec.get('retention_days')} j) : {job_count}")
        if not args.dry_run:
            audit_days = sec.get("audit_retention_days", 1095)
            if isinstance(audit_days, (int, float)) and audit_days > 0:
                n = AuditStore.purge_expired_by_policy(
                    int(audit_days), sec.get("audit_retention_by_family") or {})
                print(f"Entrées d'audit purgées : {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcria-maintenance",
        description="Sauvegarde / restauration locale de TranscrIA (C1.1).")
    parser.add_argument("--config", default=None, help="chemin de config.yaml (défaut : TRANSCRIA_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="créer une archive de sauvegarde")
    b.add_argument("--dest", default="./backups", help="dossier des archives (défaut ./backups)")
    b.add_argument("--exclude-audio", action="store_true", help="ne pas embarquer les audios originaux")
    b.add_argument("--keep", type=int, default=0, help="rotation : ne garder que N archives")
    b.add_argument("--env", default=None, help="chemin du .env (empreinte au manifeste ; défaut ./.env)")
    b.set_defaults(func=_cmd_backup)

    v = sub.add_parser("backup-verify", help="vérifier l'intégrité d'une archive")
    v.add_argument("archive")
    v.set_defaults(func=_cmd_backup_verify)

    r = sub.add_parser("restore", help="restaurer une archive")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true", help="écraser une base cible non vide")
    r.add_argument("--dry-run", action="store_true", help="lister sans rien écrire")
    r.add_argument("--ready-url", default="http://127.0.0.1:7870/ready",
                   help="URL /ready de l'instance CIBLE (garde anti-restauration à chaud)")
    r.set_defaults(func=_cmd_restore)

    u = sub.add_parser("upgrade", help="mise à niveau outillée (sauvegarde → code → migration → restart → santé)")
    u.add_argument("--ref", default=None, help="tag/branche à déployer (défaut : git pull --ff-only)")
    u.add_argument("--units", default="transcria.service", help="services systemd à redémarrer (séparés par ,)")
    u.add_argument("--ready-url", default="http://127.0.0.1:7870/ready", help="URL du contrôle de santé")
    u.add_argument("--backup-dest", default="./backups", help="dossier de la sauvegarde de sécurité")
    u.add_argument("--keep", type=int, default=0, help="rotation des sauvegardes")
    u.add_argument("--check", action="store_true", help="lister les étapes sans les exécuter")
    u.set_defaults(func=_cmd_upgrade)

    pg = sub.add_parser("purge", help="purger les données expirées (rétention DPO)")
    pg.add_argument("--dry-run", action="store_true", help="compter sans supprimer")
    pg.set_defaults(func=_cmd_purge)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

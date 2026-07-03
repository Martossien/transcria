"""Restauration LOCALE — chantier C1.1 (docs/RELEASE_0.2.0.md).

Garde-fous (le restore est irréversible — on protège l'opérateur) :
- ``--dry-run`` liste ce qui SERAIT restauré, sans rien toucher ;
- refus si la base cible n'est PAS vide, sauf ``force=True`` ;
- vérification du manifeste (format + intégrité) AVANT d'écrire quoi que ce soit ;
- les arbres de fichiers sont extraits en zone temporaire puis COPIÉS PAR-DESSUS la
  cible (fusion : les fichiers homonymes sont remplacés, les fichiers propres à la
  cible restent) — pour une reprise à l'identique, restaurez vers une cible VIERGE ;
- le ``config.yaml`` de l'archive n'écrase JAMAIS celui de la cible : il est déposé
  en ``config.restored.yaml`` à côté, à réconcilier à la main ;
- si le service répond encore (``/ready``), la restauration est REFUSÉE sauf
  ``force`` : écraser une base vivante = corruption.
"""
from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from transcria.maintenance.backup import BackupError, read_manifest, resolve_database_url, verify_backup


def _target_db(cfg: dict):
    db_url = resolve_database_url(cfg)
    if not db_url:
        raise BackupError("storage.database_url absent : impossible de cibler la restauration.")
    if db_url.startswith("sqlite"):
        return "sqlite", db_url, Path(db_url.split("sqlite:///", 1)[-1])
    return "postgresql", db_url, None


def database_is_empty(cfg: dict) -> bool:
    """True si la base cible n'a aucune table applicative (restauration sûre)."""
    from sqlalchemy import create_engine, inspect

    _, db_url, _ = _target_db(cfg)
    engine = create_engine(db_url.replace("postgresql+psycopg", "postgresql+psycopg"))
    try:
        tables = inspect(engine).get_table_names()
    finally:
        engine.dispose()
    # alembic_version seule = base migrée mais vide de données applicatives.
    return not [t for t in tables if t not in ("alembic_version",)]


def describe_restore(archive: Path) -> dict:
    """Ce que contient l'archive (pour --dry-run) — ne touche à rien."""
    manifest = read_manifest(archive)
    entries = manifest.get("entries", {})
    return {
        "created_at": manifest.get("created_at"),
        "app_version": manifest.get("app_version"),
        "alembic_revision": manifest.get("alembic_revision"),
        "db_kind": manifest.get("db_kind"),
        "include_audio": manifest.get("include_audio"),
        "trees": entries.get("trees", []),
        "has_config": "config" in entries,
    }


def _restore_database(cfg: dict, staging: Path, db_kind: str, *, runner=subprocess.run) -> None:
    target_kind, db_url, sqlite_path = _target_db(cfg)
    if target_kind != db_kind:
        raise BackupError(
            f"incompatibilité : sauvegarde {db_kind} → cible {target_kind}. "
            "Restaurez vers le même type de base.")

    if db_kind == "sqlite":
        src = staging / "database.sqlite"
        if not src.exists():
            raise BackupError("dump SQLite absent de l'archive.")
        assert sqlite_path is not None
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, sqlite_path)
        return

    dump = staging / "database.dump"
    if not dump.exists():
        raise BackupError("dump PostgreSQL absent de l'archive.")
    parsed = urlparse(db_url.replace("postgresql+psycopg", "postgresql"))
    import os

    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    args = [
        "pg_restore", "--no-owner", "--clean", "--if-exists",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        "-d", (parsed.path or "/").lstrip("/"),
        str(dump),
    ]
    proc = runner(args, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BackupError(f"pg_restore a échoué (code {proc.returncode}) : {proc.stderr.strip()[:400]}")


def _restore_tree(staging: Path, label: str, target: Path) -> None:
    src = staging / label
    if not src.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)


def _service_responds(ready_url: str, timeout: float = 2.0) -> bool:
    try:
        import requests

        return requests.get(ready_url, timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 — service éteint = comportement attendu
        return False


def restore_backup(
    cfg: dict,
    archive: Path,
    *,
    force: bool = False,
    runner=subprocess.run,
    ready_url: str = "http://127.0.0.1:7870/ready",
) -> dict:
    """Restaure une archive dans l'instance décrite par ``cfg``. Renvoie un rapport."""
    problems = verify_backup(archive)
    if problems:
        raise BackupError("archive corrompue : " + " ; ".join(problems))

    if not force and _service_responds(ready_url):
        raise BackupError(
            "le service TranscrIA répond encore (" + ready_url + ") — restaurer par-dessus "
            "une base VIVANTE risque la corruption. Arrêtez-le d'abord "
            "(sudo systemctl stop transcria) ou passez --force en connaissance de cause.")

    manifest = read_manifest(archive)
    db_kind = manifest.get("db_kind", "")

    if not force and not database_is_empty(cfg):
        raise BackupError(
            "la base cible n'est pas vide — restauration refusée. "
            "Utilisez force=True (CLI : --force) pour écraser les données existantes.")

    storage = cfg.get("storage", {})
    jobs_dir = Path(storage.get("jobs_dir") or "./jobs")
    voices_raw = cfg.get("voice_enrollment", {}).get("storage_dir")
    voices_dir = Path(voices_raw) if voices_raw else None

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")  # noqa: S202 — archive de confiance

        _restore_database(cfg, staging, db_kind, runner=runner)
        _restore_tree(staging, "jobs", jobs_dir)
        if voices_dir:
            _restore_tree(staging, "voices", voices_dir)
        _restore_tree(staging, "prompts", Path("configs/prompts"))

        # config.yaml : JAMAIS écrasé silencieusement — déposé à côté pour réconciliation.
        archived_cfg = staging / "config.yaml"
        restored_cfg_path = None
        if archived_cfg.exists():
            target_cfg = Path(str(cfg.get("_config_path") or "config.yaml"))
            restored_cfg_path = target_cfg.with_name("config.restored.yaml")
            shutil.copy2(archived_cfg, restored_cfg_path)

    return {
        "config_restored_as": str(restored_cfg_path) if restored_cfg_path else None,
        "restored_from": archive.name,
        "app_version": manifest.get("app_version"),
        "alembic_revision": manifest.get("alembic_revision"),
        "db_kind": db_kind,
        "trees": manifest.get("entries", {}).get("trees", []),
    }

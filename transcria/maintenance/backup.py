"""Sauvegarde / restauration LOCALE — chantier C1.1 (docs/archive/RELEASE_0.2.0.md).

LE manque n°1 de production : aucun mécanisme n'existait (pas un tar.gz, pas un dump).
Périmètre des données protégées :
- la base (PostgreSQL via ``pg_dump`` OU SQLite via l'API ``backup`` à chaud) ;
- ``jobs/`` (livrables, artefacts, brouillons de l'éditeur) ;
- ``voices/`` (empreintes biométriques — donnée sensible) ;
- ``config.yaml`` + prompts personnalisés ``configs/prompts/``.

Le ``.env`` n'est PAS embarqué (secrets) : son EMPREINTE seule figure au manifeste,
pour vérifier à la restauration qu'on retrouve le même environnement.

Cohérence : la base est capturée EN PREMIER, puis les fichiers — la fenêtre est
documentée (un job qui termine pendant la copie sera dans les fichiers mais pas dans
le dump ; à la restauration il apparaîtra « en cours » et pourra être relancé).

Tout passe par des fonctions PURES et testées ; la CLI (``cli.py``) n'est qu'un runner.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

MANIFEST_NAME = "manifest.json"
_BACKUP_FORMAT = 1


class BackupError(Exception):
    """Erreur de sauvegarde/restauration destinée à l'opérateur (message actionnable)."""


@dataclass
class BackupPlan:
    """Ce qu'une sauvegarde va embarquer, résolu depuis la config (testable sans I/O)."""

    db_kind: str                       # "postgresql" | "sqlite"
    db_url: str
    sqlite_path: Path | None
    jobs_dir: Path
    voices_dir: Path | None
    config_path: Path | None
    prompts_dir: Path | None
    include_audio: bool = True
    extra_notes: list[str] = field(default_factory=list)


def resolve_database_url(cfg: dict) -> str:
    """DSN de la base — MÊME précédence que app.resolve_database_uri : l'override
    d'environnement TRANSCRIA_DATABASE_URL prime sur ``storage.database_url``.
    Sans cela, une restauration ciblerait la base par défaut au lieu de l'instance
    réellement configurée par l'environnement (bug attrapé au banc E2E)."""
    import os

    return (
        os.environ.get("TRANSCRIA_DATABASE_URL")
        or str(cfg.get("storage", {}).get("database_url") or "")
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_from_config(cfg: dict, config_path: str | None, *, include_audio: bool = True) -> BackupPlan:
    """Résout le périmètre depuis la config — pur, sans toucher au disque."""
    storage = cfg.get("storage", {})
    db_url = resolve_database_url(cfg)
    if not db_url:
        raise BackupError("storage.database_url absent de la configuration.")

    if db_url.startswith("sqlite"):
        db_kind = "sqlite"
        # sqlite:///relative.db  ou  sqlite:////abs/path.db — les paramètres de
        # requête (?timeout=…) ne font PAS partie du chemin (revue qualité).
        raw = db_url.split("sqlite:///", 1)[-1].split("?", 1)[0]
        sqlite_path = Path(raw)
    else:
        db_kind = "postgresql"
        sqlite_path = None

    jobs_dir = Path(storage.get("jobs_dir") or "./jobs")
    voices_raw = cfg.get("voice_enrollment", {}).get("storage_dir")
    voices_dir = Path(voices_raw) if voices_raw else None
    config_p = Path(config_path) if config_path else None
    prompts = Path("configs/prompts")

    return BackupPlan(
        db_kind=db_kind,
        db_url=db_url,
        sqlite_path=sqlite_path,
        jobs_dir=jobs_dir,
        voices_dir=voices_dir if (voices_dir and voices_dir.exists()) else None,
        config_path=config_p if (config_p and config_p.exists()) else None,
        prompts_dir=prompts if prompts.exists() else None,
        include_audio=include_audio,
    )


def _dump_database(plan: BackupPlan, dest: Path, *, runner=subprocess.run) -> dict:
    """Écrit le dump de la base dans ``dest`` et renvoie une entrée de manifeste."""
    if plan.db_kind == "sqlite":
        if plan.sqlite_path is None or not plan.sqlite_path.exists():
            raise BackupError(f"base SQLite introuvable : {plan.sqlite_path}")
        # API sqlite3 backup : copie cohérente MÊME si l'app écrit en parallèle.
        import sqlite3

        src = sqlite3.connect(str(plan.sqlite_path))
        try:
            out = sqlite3.connect(str(dest))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        return {"kind": "sqlite", "file": dest.name, "sha256": _sha256_file(dest)}

    # PostgreSQL : pg_dump en format custom (-Fc), restaurable par pg_restore.
    parsed = urlparse(plan.db_url.replace("postgresql+psycopg", "postgresql"))
    env_args = [
        "pg_dump", "-Fc", "-f", str(dest),
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        (parsed.path or "/").lstrip("/"),
    ]
    import os

    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    proc = runner(env_args, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BackupError(f"pg_dump a échoué (code {proc.returncode}) : {proc.stderr.strip()[:400]}")
    return {"kind": "postgresql", "file": dest.name, "sha256": _sha256_file(dest)}


def _copy_tree(src: Path, dest: Path, *, include_audio: bool) -> None:
    """Copie récursive ; ``include_audio=False`` saute les originaux (input/original.*)."""
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if not include_audio and item.parent.name == "input" and item.stem == "original":
            continue
        rel = item.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def create_backup(
    cfg: dict,
    config_path: str | None,
    dest_dir: Path,
    *,
    app_version: str,
    alembic_revision: str | None,
    include_audio: bool = True,
    scope: str = "full",
    env_path: Path | None = None,
    runner=subprocess.run,
    now: datetime | None = None,
) -> Path:
    """Crée une archive tar.gz horodatée + manifeste. Renvoie le chemin de l'archive.

    ``scope`` : ``full`` (défaut, base + fichiers), ``db`` (base seule — sauvegarde
    rapide quotidienne) ou ``files`` (jobs/voix/prompts/config, sans la base). La
    restauration est pilotée par le manifeste : une archive partielle ne restaure
    que ce qu'elle contient.
    """
    if scope not in ("full", "db", "files"):
        raise BackupError(f"scope inconnu : {scope!r} (attendu : full, db ou files)")
    plan = plan_from_config(cfg, config_path, include_audio=include_audio)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        entries: dict = {"trees": []}
        # 1. base D'ABORD (cohérence : la fenêtre est côté fichiers, jamais côté base).
        if scope != "files":
            db_file = staging / ("database.sqlite" if plan.db_kind == "sqlite" else "database.dump")
            entries["database"] = _dump_database(plan, db_file, runner=runner)

        # 2. fichiers.
        if scope != "db":
            trees: list[str] = entries["trees"]
            for label, path in [("jobs", plan.jobs_dir), ("voices", plan.voices_dir),
                                ("prompts", plan.prompts_dir)]:
                if path and path.exists():
                    out = staging / label
                    _copy_tree(path, out, include_audio=plan.include_audio)
                    trees.append(label)
            if plan.config_path:
                shutil.copy2(plan.config_path, staging / "config.yaml")
                entries["config"] = "config.yaml"

        # 3. manifeste (traçabilité + garde de restauration).
        manifest = {
            "format": _BACKUP_FORMAT,
            "created_at": stamp,
            "app_version": app_version,
            "alembic_revision": alembic_revision,
            "db_kind": plan.db_kind,
            "include_audio": include_audio,
            "scope": scope,
            "entries": entries,
            "env_sha256": _sha256_file(env_path) if (env_path and env_path.exists()) else None,
            "notes": plan.extra_notes,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        infix = "" if scope == "full" else f"{scope}-"
        archive = dest_dir / f"transcria-backup-{infix}{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(staging.rglob("*")):
                if item.is_file():
                    tar.add(item, arcname=str(item.relative_to(staging)))

    archive.chmod(0o600)  # l'archive contient config + données : lisible par le seul propriétaire.
    return archive


def read_manifest(archive: Path) -> dict:
    """Lit le manifeste d'une archive sans la déballer entièrement.

    Toute erreur d'ouverture/lecture (gzip tronqué, tar corrompu, JSON illisible)
    est convertie en ``BackupError`` : un manifeste au bord d'une zone tronquée peut
    lever ``EOFError``/``tarfile.TarError`` au ``read()`` — l'appelant (``verify_backup``,
    restauration) doit recevoir une corruption *signalée*, jamais un crash brut.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember(MANIFEST_NAME)
            except KeyError as exc:
                raise BackupError(f"{archive.name} : manifeste absent — archive invalide.") from exc
            fh = tar.extractfile(member)
            if fh is None:
                raise BackupError(f"{archive.name} : manifeste illisible.")
            return json.loads(fh.read().decode("utf-8"))
    except (OSError, EOFError, tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError(f"{archive.name} : archive illisible (corrompue ?) — {exc}") from exc


def verify_backup(archive: Path) -> list[str]:
    """Intègrité : archive ouvrable + toutes les sommes sha256 du manifeste concordent.
    Renvoie la liste des problèmes (vide = archive saine)."""
    problems: list[str] = []
    if not archive.exists():
        return [f"archive introuvable : {archive}"]
    try:
        manifest = read_manifest(archive)
    except BackupError as exc:
        return [str(exc)]

    # 1. décompression gzip COMPLÈTE : force la vérification du CRC/longueur en fin de
    #    flux → une troncature ou une altération est détectée (le tar lit en streaming
    #    et peut tolérer un flux tronqué ; gzip.read() jusqu'au bout, non).
    import gzip

    try:
        with gzip.open(archive, "rb") as gz:
            while gz.read(1 << 20):
                pass
    except (OSError, EOFError) as exc:
        return [f"archive illisible (corrompue ?) : {exc}"]

    # 2. structure tar : chaque membre doit être lisible.
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    fh = tar.extractfile(member)
                    if fh is None:
                        problems.append(f"membre illisible : {member.name}")
    except (tarfile.TarError, OSError, EOFError) as exc:
        return [f"archive illisible (corrompue ?) : {exc}"]

    # 3. l'empreinte de la base doit concorder avec le manifeste.
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "r:gz") as tar:
        db = manifest.get("entries", {}).get("database", {})
        db_file = db.get("file")
        if db_file and db.get("sha256"):
            try:
                tar.extract(db_file, tmp, filter="data")  # noqa: S202 — archive de confiance
                got = _sha256_file(Path(tmp) / db_file)
                if got != db["sha256"]:
                    problems.append(f"empreinte de la base divergente ({db_file})")
            except KeyError:
                problems.append(f"fichier de base absent de l'archive ({db_file})")
    return problems


@dataclass
class RestorePlan:
    """Cible d'une restauration, résolue depuis la config courante."""

    db_kind: str
    db_url: str
    sqlite_path: Path | None
    jobs_dir: Path
    voices_dir: Path | None
    config_path: Path | None


def rotate_backups(dest_dir: Path, keep: int, *, scope: str = "full") -> list[Path]:
    """Supprime les archives les plus anciennes au-delà de ``keep``. Renvoie les supprimées.

    La rotation est PAR SCOPE (les archives ``db``/``files`` portent leur scope dans le
    nom) : une rafale de sauvegardes base-seule ne peut pas expulser la sauvegarde
    complète du pot commun.
    """
    if keep <= 0:
        return []
    pattern = "transcria-backup-[0-9]*.tar.gz" if scope == "full" else f"transcria-backup-{scope}-*.tar.gz"
    archives = sorted(dest_dir.glob(pattern))
    to_delete = archives[:-keep] if len(archives) > keep else []
    for path in to_delete:
        path.unlink()
    return to_delete

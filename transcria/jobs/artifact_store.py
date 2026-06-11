"""Magasin de fichiers de jobs partagé via PostgreSQL (topologie split web/worker).

Quand la frontale (`role=web`) et le worker GPU (`role=scheduler`) tournent sur deux
machines SANS filesystem partagé, les fichiers d'un job doivent suivre le même chemin
que son état : la base. Les `jobs_dir` locaux deviennent des **caches matérialisés** ;
la copie de référence vit dans `job_files`/`job_file_chunks` pendant la vie du job.

Activation : `storage.shared_backend: pg` (défaut `fs` = comportement historique,
aucune écriture en base). Voir docs/STOCKAGE_PARTAGE_JOBS.md.

Garanties :
- un push est transactionnel **par fichier** (tout ou rien, upsert idempotent par sha256) ;
- une matérialisation est atomique (tmp + vérification sha256 + ``os.replace``) ;
- un fichier local modifié mais non poussé n'est **jamais écrasé** par un pull
  (détection via le manifeste local ``.sync_state.json``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError

from transcria.database import db
from transcria.jobs.models import JobFile, JobFileChunk

logger = logging.getLogger(__name__)

# Taille de chunk : borne la mémoire au push/pull, même au plafond d'upload (1 Go).
CHUNK_SIZE = 8 * 1024 * 1024

# Préfixes synchronisés entre tiers. Exclus volontairement : `exports/` (zip/docx
# reconstruits localement à la demande), `audio/` (intermédiaires du préprocess,
# locaux au worker), et les caches générés à la demande (voir EXCLUDED_PREFIXES).
SYNCED_PREFIXES: tuple[str, ...] = ("input/", "context/", "metadata/", "speakers/", "quality/", "summary/")
EXCLUDED_PREFIXES: tuple[str, ...] = ("metadata/audio_excerpts/",)

# Préfixes que la frontale pousse à l'enfilage (entrées du worker).
INPUT_PREFIXES: tuple[str, ...] = ("input/", "context/", "speakers/")

MANIFEST_NAME = ".sync_state.json"


class ArtifactIntegrityError(RuntimeError):
    """Le contenu matérialisé ne correspond pas au sha256 attendu (après re-tentative)."""


def backend_name(cfg: dict) -> str:
    return str((cfg.get("storage") or {}).get("shared_backend") or "fs").strip().lower()


def is_pg_backend(cfg: dict) -> bool:
    return backend_name(cfg) == "pg"


def _job_dir(cfg: dict, job_id: str) -> Path:
    jobs_dir = (cfg.get("storage") or {}).get("jobs_dir", "./jobs")
    return Path(jobs_dir).resolve() / job_id


# ── Manifeste local (cache d'état de synchro, par machine) ──────────────────────────


def _load_manifest(job_dir: Path) -> dict:
    path = job_dir / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("Manifeste de synchro illisible (reconstruit) : %s", path)
        return {}


def _save_manifest(job_dir: Path, manifest: dict) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(job_dir), prefix=f".{MANIFEST_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, job_dir / MANIFEST_NAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _manifest_entry(st: os.stat_result, sha256: str) -> dict:
    return {"sha256": sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _stat_matches(entry: dict | None, st: os.stat_result) -> bool:
    if not entry:
        return False
    return entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns


# ── Utilitaires fichiers ─────────────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


def _is_excluded(relpath: str) -> bool:
    return any(relpath.startswith(p) for p in EXCLUDED_PREFIXES)


def _iter_local_files(job_dir: Path, prefixes: Iterable[str]) -> Iterator[tuple[str, Path]]:
    """Itère (relpath posix, chemin absolu) des fichiers locaux sous les préfixes donnés.

    Ignore les fichiers cachés/temporaires (noms commençant par `.`, dont le manifeste
    et les tmp d'écriture atomique) et les liens symboliques.
    """
    for prefix in prefixes:
        base = job_dir / prefix.rstrip("/")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            relpath = path.relative_to(job_dir).as_posix()
            if not _is_excluded(relpath):
                yield relpath, path


def _matches_prefixes(relpath: str, prefixes: Iterable[str]) -> bool:
    return any(relpath.startswith(p) for p in prefixes)


def newest_synced_mtime_ns(cfg: dict, job_id: str) -> int:
    """mtime (ns) du fichier synchronisé le plus récent du job sur CE disque.

    Sert au test de fraîcheur des livrables reconstruits localement (ex. package zip,
    exclu de la synchro car il contient l'audio)."""
    newest = 0
    for _relpath, path in _iter_local_files(_job_dir(cfg, job_id), SYNCED_PREFIXES):
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            continue
    return newest


# ── Push : disque local → base ───────────────────────────────────────────────────────


def push_job_files(
    cfg: dict,
    job_id: str,
    *,
    prefixes: Iterable[str] = SYNCED_PREFIXES,
    chunk_size: int = CHUNK_SIZE,
) -> dict:
    """Pousse en base les fichiers locaux nouveaux/modifiés (idempotent, sha256).

    No-op (stats à zéro) si `storage.shared_backend` n'est pas `pg`.
    Une erreur de push DOIT remonter : l'appelant ne doit pas considérer ses
    artefacts comme durables (ex. ne pas marquer une phase « faite »).
    """
    if not is_pg_backend(cfg):
        return {"backend": backend_name(cfg), "pushed": 0, "skipped": 0, "bytes": 0}

    t0 = time.monotonic()
    job_dir = _job_dir(cfg, job_id)
    manifest = _load_manifest(job_dir)
    db_shas: dict[str, str] = {
        relpath: sha
        for relpath, sha in db.session.query(JobFile.relpath, JobFile.sha256).filter(
            JobFile.job_id == job_id
        )
    }

    pushed = 0
    skipped = 0
    total_bytes = 0
    manifest_dirty = False
    for relpath, path in _iter_local_files(job_dir, prefixes):
        try:
            st = path.stat()
        except OSError:
            continue
        entry = manifest.get(relpath)
        if entry and _stat_matches(entry, st) and db_shas.get(relpath) == entry.get("sha256"):
            skipped += 1
            continue
        sha = _hash_file(path)
        if db_shas.get(relpath) == sha:
            manifest[relpath] = _manifest_entry(st, sha)
            manifest_dirty = True
            skipped += 1
            continue
        _upsert_file(job_id, relpath, path, sha, st.st_size, chunk_size)
        manifest[relpath] = _manifest_entry(st, sha)
        manifest_dirty = True
        pushed += 1
        total_bytes += st.st_size

    if manifest_dirty:
        _save_manifest(job_dir, manifest)
    duration_ms = round((time.monotonic() - t0) * 1000)
    if pushed:
        logger.info(
            "Artefacts poussés en base: job=%s fichiers=%d octets=%d ignorés=%d durée=%dms",
            job_id, pushed, total_bytes, skipped, duration_ms,
        )
    return {"backend": "pg", "pushed": pushed, "skipped": skipped, "bytes": total_bytes}


def _upsert_file(job_id: str, relpath: str, path: Path, sha: str, size: int, chunk_size: int) -> None:
    """Remplace le contenu d'un fichier en base — transaction par fichier (tout ou rien)."""
    try:
        _upsert_file_once(job_id, relpath, path, sha, size, chunk_size)
    except IntegrityError:
        # Course entre deux pousseurs sur (job_id, relpath) : l'autre a créé la ligne.
        db.session.rollback()
        _upsert_file_once(job_id, relpath, path, sha, size, chunk_size)


def _upsert_file_once(job_id: str, relpath: str, path: Path, sha: str, size: int, chunk_size: int) -> None:
    try:
        row = (
            db.session.query(JobFile)
            .filter(JobFile.job_id == job_id, JobFile.relpath == relpath)
            .one_or_none()
        )
        if row is None:
            row = JobFile(job_id=job_id, relpath=relpath, sha256=sha, size_bytes=0, chunk_count=0)
            db.session.add(row)
            db.session.flush()
        else:
            db.session.execute(sa_delete(JobFileChunk).where(JobFileChunk.file_id == row.id))

        seq = 0
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk_size), b""):
                db.session.execute(sa_insert(JobFileChunk).values(file_id=row.id, seq=seq, data=block))
                seq += 1
        row.sha256 = sha
        row.size_bytes = size
        row.chunk_count = seq
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


# ── Pull : base → disque local (matérialisation) ─────────────────────────────────────


def pull_job_files(
    cfg: dict,
    job_id: str,
    *,
    prefixes: Iterable[str] = SYNCED_PREFIXES,
) -> dict:
    """Matérialise localement les fichiers de la base (atomique, sha256 vérifié).

    Règle de protection : un fichier local dont l'état ne correspond plus au manifeste
    (modifications locales non poussées) n'est JAMAIS écrasé — le push réconciliera.
    """
    if not is_pg_backend(cfg):
        return {"backend": backend_name(cfg), "pulled": 0, "skipped": 0, "bytes": 0}

    t0 = time.monotonic()
    job_dir = _job_dir(cfg, job_id)
    rows = (
        db.session.query(JobFile.id, JobFile.relpath, JobFile.sha256, JobFile.size_bytes, JobFile.chunk_count)
        .filter(JobFile.job_id == job_id)
        .all()
    )
    manifest = _load_manifest(job_dir)

    pulled = 0
    skipped = 0
    total_bytes = 0
    manifest_dirty = False
    for file_id, relpath, sha, size_bytes, chunk_count in rows:
        if not _matches_prefixes(relpath, prefixes) or _is_excluded(relpath):
            continue
        local = job_dir / relpath
        entry = manifest.get(relpath)
        if entry and entry.get("sha256") == sha and local.is_file() and local.stat().st_size == size_bytes:
            skipped += 1
            continue
        if local.is_file():
            st = local.stat()
            if entry is None:
                # Fichier local hors manifeste (legacy / écrit sans push) : on l'adopte
                # s'il est identique, sinon on ne détruit rien — signalé pour arbitrage.
                local_sha = _hash_file(local)
                if local_sha == sha:
                    manifest[relpath] = _manifest_entry(st, sha)
                    manifest_dirty = True
                    skipped += 1
                    continue
                logger.warning(
                    "Conflit de synchro (fichier local hors manifeste, contenu différent — non écrasé): "
                    "job=%s fichier=%s", job_id, relpath,
                )
                skipped += 1
                continue
            if not _stat_matches(entry, st):
                logger.warning(
                    "Pull ignoré (modifications locales non poussées — non écrasé): job=%s fichier=%s",
                    job_id, relpath,
                )
                skipped += 1
                continue
        _materialize(job_id, file_id, relpath, sha, chunk_count, local)
        manifest[relpath] = _manifest_entry(local.stat(), sha)
        manifest_dirty = True
        pulled += 1
        total_bytes += size_bytes

    if manifest_dirty:
        _save_manifest(job_dir, manifest)
    duration_ms = round((time.monotonic() - t0) * 1000)
    if pulled:
        logger.info(
            "Artefacts matérialisés depuis la base: job=%s fichiers=%d octets=%d durée=%dms",
            job_id, pulled, total_bytes, duration_ms,
        )
    return {"backend": "pg", "pulled": pulled, "skipped": skipped, "bytes": total_bytes}


def _materialize(job_id: str, file_id: int, relpath: str, sha: str, chunk_count: int, dest: Path) -> None:
    """Écrit le contenu base → `dest` (tmp + sha256 + os.replace). Une re-tentative si la
    lecture a croisé un upsert concurrent (la ligne est alors relue)."""
    try:
        _materialize_once(file_id, sha, chunk_count, dest)
    except ArtifactIntegrityError:
        row = db.session.query(JobFile).filter(JobFile.id == file_id).one_or_none()
        if row is None:
            raise
        _materialize_once(file_id, row.sha256, row.chunk_count, dest)
    logger.debug("Fichier matérialisé: job=%s fichier=%s", job_id, relpath)


def _materialize_once(file_id: int, sha: str, chunk_count: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".tmp")
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as fh:
            # SELECT par chunk : mémoire bornée quelle que soit la taille du fichier.
            for seq in range(chunk_count):
                data = (
                    db.session.query(JobFileChunk.data)
                    .filter(JobFileChunk.file_id == file_id, JobFileChunk.seq == seq)
                    .scalar()
                )
                if data is None:
                    raise ArtifactIntegrityError(f"chunk {seq} manquant (file_id={file_id})")
                fh.write(data)
                h.update(data)
            fh.flush()
            os.fsync(fh.fileno())
        if h.hexdigest() != sha:
            raise ArtifactIntegrityError(f"sha256 inattendu pour file_id={file_id}")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Pull paresseux côté frontale (throttle par job) ──────────────────────────────────

_PULL_THROTTLE_S = 2.0
_last_pull: dict[str, float] = {}
_last_pull_lock = threading.Lock()


def pull_job_files_throttled(cfg: dict, job_id: str, *, min_interval_s: float = _PULL_THROTTLE_S) -> None:
    """Pull paresseux (avant requête web) : au plus un pull par job par fenêtre.

    Best-effort : une erreur est loguée mais ne bloque jamais la requête HTTP.
    """
    if not is_pg_backend(cfg):
        return
    now = time.monotonic()
    with _last_pull_lock:
        last = _last_pull.get(job_id, 0.0)
        if now - last < min_interval_s:
            return
        _last_pull[job_id] = now
        # Borne la taille du cache de throttle (process long-vivant, jobs nombreux).
        if len(_last_pull) > 2048:
            cutoff = now - max(min_interval_s, 60.0)
            for key in [k for k, v in _last_pull.items() if v < cutoff]:
                _last_pull.pop(key, None)
    try:
        pull_job_files(cfg, job_id)
    except Exception:
        logger.exception("Pull paresseux des artefacts impossible: job=%s", job_id)


# ── Purge / suppression ──────────────────────────────────────────────────────────────


def purge_input_files(cfg: dict, job_id: str) -> int:
    """Supprime les blobs `input/` (le poids lourd) en fin de vie d'exécution.

    L'original reste sur le disque de la frontale (origine) : un reprocess re-pousse
    `input/` à l'enfilage (`submit_process`). Les artefacts (Ko–Mo) restent en base
    pour la matérialisation paresseuse des frontales.
    """
    if not is_pg_backend(cfg):
        return 0
    return _delete_files(job_id, prefix="input/")


def delete_job_files(job_id: str) -> int:
    """Purge totale des blobs d'un job (suppression du job). Inconditionnel : la table
    peut contenir des lignes d'une période en backend `pg` même si la config a changé."""
    return _delete_files(job_id, prefix=None)


def _delete_files(job_id: str, *, prefix: str | None) -> int:
    try:
        query = db.session.query(JobFile.id).filter(JobFile.job_id == job_id)
        if prefix is not None:
            query = query.filter(JobFile.relpath.like(f"{prefix}%"))
        file_ids = [fid for (fid,) in query.all()]
        if not file_ids:
            return 0
        db.session.execute(sa_delete(JobFileChunk).where(JobFileChunk.file_id.in_(file_ids)))
        db.session.execute(sa_delete(JobFile).where(JobFile.id.in_(file_ids)))
        db.session.commit()
        logger.info(
            "Blobs supprimés: job=%s fichiers=%d périmètre=%s", job_id, len(file_ids), prefix or "tous",
        )
        return len(file_ids)
    except Exception:
        db.session.rollback()
        raise

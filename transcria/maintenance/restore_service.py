"""Restauration depuis l'UI via un one-shot systemd PRIVILÉGIÉ (C1.4).

Problème : restaurer par-dessus une instance VIVANTE corrompt la base (le garde
``restore_backup`` le refuse). Or l'UI tourne DANS l'instance. Solution : l'UI ne restaure
pas elle-même — elle **dépose une demande** puis déclenche l'unité oneshot
``transcria-restore.service`` (``User=root``) qui **arrête le service → restaure (force) →
rechown → redémarre**. Le web ne fait qu'un ``systemctl start --no-block`` (retour immédiat ;
le worker est tué avec le service, la restauration continue seule).

Tout est injectable (``run``/``write``) pour être testé sans systemd ni privilèges.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from transcria.maintenance.backup import BackupError

RESTORE_UNIT = "transcria-restore.service"
REQUEST_PATH = Path("/run/transcria-restore.request")
DEFAULT_UNITS = "transcria.service"
DEFAULT_UNITS_DIR = Path("/etc/systemd/system")

RunFn = Callable[..., subprocess.CompletedProcess]
WriteFn = Callable[[Path, str], None]


def _default_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def resolve_archive_in(backup_dir: Path, name: str) -> Path | None:
    """Résout un nom d'archive DANS ``backup_dir`` (anti path-traversal), ou ``None``."""
    directory = backup_dir.resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory:
        return None
    if not candidate.name.startswith("transcria-backup-") or not candidate.name.endswith(".tar.gz"):
        return None
    return candidate if candidate.is_file() else None


def render_restore_unit(*, install_dir: str, python_bin: str, config_path: str,
                        env_file: str, units: str) -> str:
    """Unité oneshot privilégiée (``User=root`` : systemctl stop/start + écriture partout).
    PAS de section [Install] : elle est déclenchée à la demande, jamais activée au boot."""
    return (
        "[Unit]\n"
        "Description=TranscrIA — restauration de sauvegarde (oneshot privilégié)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        f"WorkingDirectory={install_dir}\n"
        f"EnvironmentFile={env_file}\n"
        f"ExecStart={python_bin} -m transcria.maintenance.cli --config {config_path} "
        f"restore-apply --units {units}\n"
    )


def ensure_restore_unit(
    unit_text: str,
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> bool:
    """Écrit l'unité si absente/différente et recharge systemd. Retourne True si (ré)écrite."""
    path = units_dir / RESTORE_UNIT
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == unit_text:
        return False
    write(path, unit_text)
    run(["systemctl", "daemon-reload"], check=True)
    return True


def request_restore(
    *,
    install_dir: str,
    python_bin: str,
    config_path: str,
    env_file: str,
    archive_name: str,
    units: str = DEFAULT_UNITS,
    request_path: Path = REQUEST_PATH,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> None:
    """Prépare l'unité, dépose la demande (nom d'archive) et déclenche le oneshot (non bloquant).

    L'appelant a DÉJÀ validé/vérifié l'archive. On ne bloque pas : ``--no-block`` rend la main
    tout de suite (le service — donc ce worker — va s'arrêter puis redémarrer)."""
    unit_text = render_restore_unit(install_dir=install_dir, python_bin=python_bin,
                                    config_path=config_path, env_file=env_file, units=units)
    ensure_restore_unit(unit_text, units_dir=units_dir, run=run, write=write)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    write(request_path, archive_name + "\n")
    run(["systemctl", "start", "--no-block", RESTORE_UNIT], check=True)


def _dir_owner(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return st.st_uid, st.st_gid
    except OSError:
        return None


def _chown_tree(path: Path, uid_gid: tuple[int, int]) -> None:
    uid, gid = uid_gid
    for root, dirs, files in os.walk(path):
        for name in [".", *dirs, *files]:
            target = Path(root) if name == "." else Path(root) / name
            try:
                os.chown(target, uid, gid)
            except OSError:
                pass


def apply_pending_restore(
    cfg: dict,
    *,
    units: str = DEFAULT_UNITS,
    request_path: Path = REQUEST_PATH,
    run: RunFn = subprocess.run,
    restore_fn: Callable[..., dict] | None = None,
    chown: bool = True,
) -> dict:
    """Applique la restauration en attente : arrête le service → restaure (force) → rechown les
    arbres restaurés vers leur propriétaire d'origine (root a écrit) → redémarre. Idempotent sur
    la demande (consommée). Lève ``BackupError`` si rien en attente / archive invalide."""
    if restore_fn is None:
        from transcria.maintenance.restore import restore_backup
        restore_fn = restore_backup

    if not request_path.exists():
        raise BackupError("aucune demande de restauration en attente")
    name = request_path.read_text(encoding="utf-8").strip()
    backup_dir = Path((cfg.get("maintenance", {}) or {}).get("backup_dir") or "./backups")
    archive = resolve_archive_in(backup_dir, name)
    if archive is None:
        request_path.unlink(missing_ok=True)
        raise BackupError(f"archive introuvable ou invalide : {name}")

    storage = cfg.get("storage", {}) or {}
    jobs_dir = Path(storage.get("jobs_dir") or "./jobs")
    voices_raw = (cfg.get("voice_enrollment", {}) or {}).get("storage_dir")
    trees = [jobs_dir] + ([Path(voices_raw)] if voices_raw else [])
    owners = {t: _dir_owner(t) for t in trees}  # propriétaires AVANT écrasement par root

    unit_list = [u for u in units.split(",") if u]
    for unit in unit_list:
        run(["systemctl", "stop", unit], check=False)  # service à l'arrêt → garde /ready OK
    try:
        report = restore_fn(cfg, archive, force=True)
        if chown:
            for tree, owner in owners.items():
                if owner and tree.exists():
                    _chown_tree(tree, owner)
    finally:
        for unit in unit_list:
            run(["systemctl", "start", unit], check=False)  # TOUJOURS relancer le service
    request_path.unlink(missing_ok=True)
    return report

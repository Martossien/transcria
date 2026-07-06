"""Service web de la page de maintenance (sauvegardes).

Orchestration LÉGÈRE au-dessus de la CLI ``transcria.maintenance.cli`` (déjà testée) : le
worker web NE bloque JAMAIS — une sauvegarde peut durer plusieurs minutes (pg_dump + tar des
jobs), donc on la lance en **sous-processus détaché** (survit à un redémarrage du worker) et la
page se contente de lister l'état des archives. La restauration (destructive) est gérée à part
(one-shot systemd), pas ici.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ARCHIVE_GLOB = "transcria-backup-*.tar.gz"


class MaintenanceService:
    @staticmethod
    def backup_dir(cfg: dict) -> Path:
        raw = (cfg.get("maintenance", {}) or {}).get("backup_dir") or "./backups"
        return Path(raw)

    @staticmethod
    def list_archives(cfg: dict) -> list[dict]:
        """Archives présentes (plus récentes d'abord) : nom, taille Mo, date de modification."""
        directory = MaintenanceService.backup_dir(cfg)
        archives: list[dict] = []
        if not directory.is_dir():
            return archives
        for path in sorted(directory.glob(_ARCHIVE_GLOB), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = path.stat()
            archives.append({
                "name": path.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 1),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            })
        return archives

    @staticmethod
    def resolve_archive(cfg: dict, name: str) -> Path | None:
        """Résout un nom d'archive DANS le dossier de sauvegarde (anti path-traversal).

        Rejette tout ce qui sort du dossier, ne matche pas le motif d'archive, ou n'existe pas."""
        directory = MaintenanceService.backup_dir(cfg).resolve()
        candidate = (directory / name).resolve()
        if candidate.parent != directory:
            return None
        if not candidate.name.startswith("transcria-backup-") or not candidate.name.endswith(".tar.gz"):
            return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def start_backup(
        cfg: dict,
        config_path: str | None,
        *,
        exclude_audio: bool,
        keep: int,
        popen=subprocess.Popen,
    ) -> Path:
        """Lance une sauvegarde en sous-processus DÉTACHÉ (CLI). Retourne le fichier de log."""
        dest = MaintenanceService.backup_dir(cfg)
        dest.mkdir(parents=True, exist_ok=True)
        log_path = dest / f".backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log"

        cmd = [sys.executable, "-m", "transcria.maintenance.cli"]
        if config_path:
            cmd += ["--config", str(config_path)]  # option globale AVANT la sous-commande
        cmd += ["backup", "--dest", str(dest)]
        if keep:
            cmd += ["--keep", str(keep)]
        if exclude_audio:
            cmd.append("--exclude-audio")

        with open(log_path, "wb") as log_file:
            # start_new_session : la sauvegarde survit à un redémarrage/HUP du worker web.
            popen(cmd, stdout=log_file, stderr=log_file, start_new_session=True, cwd=os.getcwd())
        return log_path

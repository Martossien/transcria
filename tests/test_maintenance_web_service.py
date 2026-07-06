"""Unitaires du service web MaintenanceService (listing, anti path-traversal, lancement CLI)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from transcria.web.maintenance_service import MaintenanceService


def _cfg(directory) -> dict:
    return {"maintenance": {"backup_dir": str(directory)}}


def test_list_archives_sorted_recent_first_with_size(tmp_path: Path):
    old = tmp_path / "transcria-backup-20260101-000000.tar.gz"
    old.write_bytes(b"x" * 1024 * 1024)
    new = tmp_path / "transcria-backup-20260201-000000.tar.gz"
    new.write_bytes(b"y" * 2 * 1024 * 1024)
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    archives = MaintenanceService.list_archives(_cfg(tmp_path))
    assert [a["name"] for a in archives] == [new.name, old.name]
    assert archives[0]["size_mb"] == 2.0


def test_list_archives_missing_dir_is_empty():
    assert MaintenanceService.list_archives({"maintenance": {"backup_dir": "/nope/xyz"}}) == []


def test_resolve_archive_valid(tmp_path: Path):
    archive = tmp_path / "transcria-backup-20260101-000000.tar.gz"
    archive.write_bytes(b"x")
    assert MaintenanceService.resolve_archive(_cfg(tmp_path), archive.name) == archive.resolve()


def test_resolve_archive_rejects_path_traversal(tmp_path: Path):
    (tmp_path.parent / "transcria-backup-secret.tar.gz").write_bytes(b"s")
    assert MaintenanceService.resolve_archive(_cfg(tmp_path), "../transcria-backup-secret.tar.gz") is None


def test_resolve_archive_rejects_wrong_pattern(tmp_path: Path):
    (tmp_path / "evil.tar.gz").write_bytes(b"x")
    assert MaintenanceService.resolve_archive(_cfg(tmp_path), "evil.tar.gz") is None


def test_resolve_archive_missing_file_returns_none(tmp_path: Path):
    assert MaintenanceService.resolve_archive(_cfg(tmp_path), "transcria-backup-20260101-000000.tar.gz") is None


def test_start_backup_launches_detached_cli(tmp_path: Path):
    calls: dict = {}

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None, cwd=None):
        calls["cmd"] = cmd
        calls["detached"] = start_new_session

        class _P:
            pid = 4242

        return _P()

    log = MaintenanceService.start_backup(
        _cfg(tmp_path), "/etc/transcria/config.yaml",
        exclude_audio=True, keep=5, popen=fake_popen,
    )
    cmd = calls["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "transcria.maintenance.cli"]
    assert "--config" in cmd and "/etc/transcria/config.yaml" in cmd
    # option globale --config AVANT la sous-commande backup
    assert cmd.index("--config") < cmd.index("backup")
    assert cmd[cmd.index("backup"):] == ["backup", "--dest", str(tmp_path), "--keep", "5", "--exclude-audio"]
    assert calls["detached"] is True
    assert log.exists() and log.name.startswith(".backup-")

"""Restauration one-shot privilégiée : rendu d'unité, ensure, demande, application ordonnée."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from transcria.maintenance.backup import BackupError
from transcria.maintenance.restore_service import (
    RESTORE_UNIT,
    apply_pending_restore,
    ensure_restore_unit,
    render_restore_unit,
    request_restore,
    resolve_archive_in,
)


def _ok(cmd, **_kw):
    return subprocess.CompletedProcess(cmd, 0)


def test_resolve_archive_in_valid(tmp_path: Path):
    a = tmp_path / "transcria-backup-20260101-000000.tar.gz"
    a.write_bytes(b"x")
    assert resolve_archive_in(tmp_path, a.name) == a.resolve()


def test_resolve_archive_in_rejects_traversal(tmp_path: Path):
    assert resolve_archive_in(tmp_path, "../etc/passwd") is None


def test_resolve_archive_in_rejects_wrong_pattern(tmp_path: Path):
    (tmp_path / "evil.tar.gz").write_bytes(b"x")
    assert resolve_archive_in(tmp_path, "evil.tar.gz") is None


def test_render_restore_unit_is_privileged_oneshot():
    txt = render_restore_unit(install_dir="/opt/x", python_bin="/py",
                              config_path="/c.yaml", env_file="/opt/x/.env",
                              units="a.service,b.service")
    assert "Type=oneshot" in txt and "User=root" in txt
    assert ("ExecStart=/py -m transcria.maintenance.cli --config /c.yaml "
            "restore-apply --units a.service,b.service") in txt
    assert "[Install]" not in txt  # jamais activée au boot — déclenchée à la demande


def test_ensure_restore_unit_writes_and_reloads(tmp_path: Path):
    calls: list = []
    written: dict = {}
    changed = ensure_restore_unit(
        "UNIT-TEXT", units_dir=tmp_path,
        run=lambda c, **k: calls.append(c) or _ok(c),
        write=lambda p, c: written.__setitem__(p.name, c),
    )
    assert changed is True
    assert written[RESTORE_UNIT] == "UNIT-TEXT"
    assert ["systemctl", "daemon-reload"] in calls


def test_ensure_restore_unit_skips_when_identical(tmp_path: Path):
    (tmp_path / RESTORE_UNIT).write_text("UNIT-TEXT", encoding="utf-8")
    calls: list = []
    changed = ensure_restore_unit("UNIT-TEXT", units_dir=tmp_path,
                                  run=lambda c, **k: calls.append(c) or _ok(c),
                                  write=lambda p, c: None)
    assert changed is False and calls == []


def test_request_restore_ensures_unit_writes_request_and_triggers(tmp_path: Path):
    calls: list = []
    written: dict = {}
    req = tmp_path / "req"
    request_restore(
        install_dir="/opt/x", python_bin="/py", config_path="/c.yaml", env_file="/opt/x/.env",
        archive_name="transcria-backup-20260101-000000.tar.gz",
        request_path=req, units_dir=tmp_path,
        run=lambda c, **k: calls.append(c) or _ok(c),
        write=lambda p, c: written.__setitem__(str(p), c),
    )
    assert "restore-apply" in written[str(tmp_path / RESTORE_UNIT)]
    assert written[str(req)].strip() == "transcria-backup-20260101-000000.tar.gz"
    assert ["systemctl", "start", "--no-block", RESTORE_UNIT] in calls


def test_apply_pending_restore_stops_restores_then_starts(tmp_path: Path):
    backup = tmp_path / "backups"
    backup.mkdir()
    archive = backup / "transcria-backup-20260101-000000.tar.gz"
    archive.write_bytes(b"x")
    req = tmp_path / "req"
    req.write_text(archive.name + "\n", encoding="utf-8")
    cfg = {"maintenance": {"backup_dir": str(backup)}, "storage": {"jobs_dir": str(tmp_path / "jobs")}}
    order: list = []

    def run(cmd, **_kw):
        order.append(("run", cmd))
        return _ok(cmd)

    def restore_fn(_cfg, arch, *, force):
        order.append(("restore", arch, force))
        return {"restored_from": arch.name, "app_version": "1.0", "db_kind": "sqlite"}

    report = apply_pending_restore(cfg, units="transcria.service", request_path=req,
                                   run=run, restore_fn=restore_fn, chown=False)
    assert report["restored_from"] == archive.name
    i_stop = order.index(("run", ["systemctl", "stop", "transcria.service"]))
    i_restore = next(i for i, e in enumerate(order) if e[0] == "restore")
    i_start = order.index(("run", ["systemctl", "start", "transcria.service"]))
    assert i_stop < i_restore < i_start           # arrêt AVANT, redémarrage APRÈS
    assert order[i_restore][2] is True            # restore forcé (service à l'arrêt)
    assert not req.exists()                        # demande consommée


def test_apply_pending_restore_no_request_raises(tmp_path: Path):
    with pytest.raises(BackupError):
        apply_pending_restore({"maintenance": {"backup_dir": str(tmp_path)}},
                              request_path=tmp_path / "absent", run=_ok, restore_fn=lambda *a, **k: {})


def test_apply_pending_restore_invalid_archive_cleans_request(tmp_path: Path):
    backup = tmp_path / "backups"
    backup.mkdir()
    req = tmp_path / "req"
    req.write_text("../evil.tar.gz\n", encoding="utf-8")
    cfg = {"maintenance": {"backup_dir": str(backup)}, "storage": {}}
    started: list = []
    with pytest.raises(BackupError):
        apply_pending_restore(cfg, request_path=req,
                              run=lambda c, **k: started.append(c) or _ok(c),
                              restore_fn=lambda *a, **k: {})
    assert not req.exists()      # demande invalide consommée
    assert started == []         # aucun service touché (échec avant l'arrêt)

"""Routes admin de maintenance : contrôle d'accès, rendu, déclenchement backup, garde download."""
from __future__ import annotations

from pathlib import Path


def test_maintenance_page_forbidden_for_viewer(viewer_client):
    assert viewer_client.get("/admin/maintenance").status_code == 403


def test_maintenance_page_renders_for_admin(admin_client):
    resp = admin_client.get("/admin/maintenance")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Maintenance" in body and "Sauvegarder maintenant" in body


def test_backup_post_triggers_start_backup(admin_client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(
        "transcria.web.maintenance_service.MaintenanceService.start_backup",
        lambda cfg, config_path, *, exclude_audio, keep, **_kw: calls.update(
            exclude_audio=exclude_audio, keep=keep) or Path("/tmp/x.log"),
    )
    resp = admin_client.post("/admin/maintenance/backup",
                             data={"keep": "3", "exclude_audio": "on"}, follow_redirects=False)
    assert resp.status_code == 302
    assert calls == {"exclude_audio": True, "keep": 3}


def test_backup_post_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/maintenance/backup", data={}).status_code == 403


def test_download_unknown_archive_is_404(admin_client):
    resp = admin_client.get("/admin/maintenance/backup/transcria-backup-00000000-000000.tar.gz/download")
    assert resp.status_code == 404


def test_schedule_enable_triggers_install(admin_client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr("transcria.maintenance.schedule.install_backup_schedule",
                        lambda schedule, **_kw: calls.setdefault("enabled", True) or ["ok"])
    resp = admin_client.post("/admin/maintenance/schedule", data={"action": "enable"})
    assert resp.status_code == 302
    assert calls.get("enabled") is True


def test_schedule_disable_triggers_remove(admin_client, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr("transcria.maintenance.schedule.remove_backup_schedule",
                        lambda **_kw: calls.setdefault("removed", True) or ["ok"])
    resp = admin_client.post("/admin/maintenance/schedule", data={"action": "disable"})
    assert resp.status_code == 302
    assert calls.get("removed") is True


def test_schedule_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/maintenance/schedule", data={"action": "enable"}).status_code == 403

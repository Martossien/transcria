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


def test_restore_requires_acknowledge(admin_client, monkeypatch):
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.restore_service.request_restore",
                        lambda **_k: triggered.setdefault("hit", True))
    resp = admin_client.post("/admin/maintenance/restore",
                             data={"name": "transcria-backup-x.tar.gz", "confirm_name": "transcria-backup-x.tar.gz"})
    assert resp.status_code == 302
    assert "hit" not in triggered  # pas de case cochée → aucun déclenchement


def test_restore_confirm_name_must_match(admin_client, monkeypatch):
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.restore_service.request_restore",
                        lambda **_k: triggered.setdefault("hit", True))
    resp = admin_client.post("/admin/maintenance/restore",
                             data={"name": "transcria-backup-x.tar.gz", "confirm_name": "WRONG", "acknowledge": "on"})
    assert resp.status_code == 302
    assert "hit" not in triggered


def test_restore_unknown_archive_404(admin_client):
    name = "transcria-backup-00000000-000000.tar.gz"
    resp = admin_client.post("/admin/maintenance/restore",
                             data={"name": name, "confirm_name": name, "acknowledge": "on"})
    assert resp.status_code == 404


def test_restore_success_triggers_request(admin_client, monkeypatch, tmp_path):
    fake = tmp_path / "transcria-backup-20260101-000000.tar.gz"
    fake.write_bytes(b"x")
    monkeypatch.setattr("transcria.web.maintenance_service.MaintenanceService.resolve_archive",
                        lambda cfg, name: fake if name == fake.name else None)
    monkeypatch.setattr("transcria.maintenance.backup.verify_backup", lambda a: [])
    called: dict = {}
    monkeypatch.setattr("transcria.maintenance.restore_service.request_restore",
                        lambda **kw: called.update(kw))
    resp = admin_client.post("/admin/maintenance/restore",
                             data={"name": fake.name, "confirm_name": fake.name, "acknowledge": "on"})
    assert resp.status_code == 302
    assert called.get("archive_name") == fake.name


def test_restore_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/maintenance/restore", data={}).status_code == 403


# --- Détection de nouvelle version (carte « Version » + bandeau admin) ---------------


def test_update_check_post_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/maintenance/update-check").status_code == 403


def test_update_check_post_reports_newer(admin_client, monkeypatch):
    monkeypatch.setattr("transcria.maintenance.update_check.check_for_update",
                        lambda cfg, **_kw: {"tag": "v99.0.0", "url": "u", "published_at": "", "notes": ""})
    resp = admin_client.post("/admin/maintenance/update-check", follow_redirects=True)
    assert resp.status_code == 200
    assert "Nouvelle version disponible" in resp.get_data(as_text=True)


def test_update_check_post_reports_up_to_date(admin_client, monkeypatch):
    from transcria import __version__
    monkeypatch.setattr("transcria.maintenance.update_check.check_for_update",
                        lambda cfg, **_kw: {"tag": f"v{__version__}", "url": "u",
                                            "published_at": "", "notes": ""})
    resp = admin_client.post("/admin/maintenance/update-check", follow_redirects=True)
    assert resp.status_code == 200
    assert "à jour" in resp.get_data(as_text=True)


def test_update_check_post_network_failure_is_actionable(admin_client, monkeypatch):
    from transcria.maintenance.update_check import UpdateCheckError

    def boom(cfg, **_kw):
        raise UpdateCheckError("API injoignable")
    monkeypatch.setattr("transcria.maintenance.update_check.check_for_update", boom)
    resp = admin_client.post("/admin/maintenance/update-check", follow_redirects=True)
    assert resp.status_code == 200
    assert "Vérification impossible" in resp.get_data(as_text=True)


def test_maintenance_page_shows_version_card_without_network(admin_client, monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("appel réseau interdit au rendu (opt-in désactivé)")
    monkeypatch.setattr("transcria.maintenance.update_check.check_for_update", forbidden)
    from transcria import __version__
    resp = admin_client.get("/admin/maintenance")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Vérifier maintenant" in body and __version__ in body


def test_maintenance_page_auto_refreshes_when_opted_in(admin_client, monkeypatch):
    from transcria.services.config_service import ConfigService
    cfg = ConfigService.get_singleton()
    monkeypatch.setitem(cfg.setdefault("maintenance", {}), "update_check", {"enabled": True})
    monkeypatch.setattr("transcria.maintenance.update_check.read_cache", lambda path: None)
    called: dict = {}

    def fake_check(c, **_kw):
        called["hit"] = True
        return {"tag": "v99.0.0", "url": "u", "published_at": "", "notes": ""}
    monkeypatch.setattr("transcria.maintenance.update_check.check_for_update", fake_check)
    resp = admin_client.get("/admin/maintenance")
    assert resp.status_code == 200
    assert called.get("hit") is True


def test_update_banner_visible_for_admin_when_cache_says_newer(admin_client, monkeypatch):
    monkeypatch.setattr("transcria.maintenance.update_check.read_cache",
                        lambda path: {"tag": "v99.0.0", "url": "u",
                                      "checked_at": "2026-08-04T00:00:00+00:00"})
    body = admin_client.get("/admin/maintenance").get_data(as_text=True)
    assert "Nouvelle version" in body and "v99.0.0" in body


def test_update_banner_absent_for_viewer(viewer_client, monkeypatch):
    monkeypatch.setattr("transcria.maintenance.update_check.read_cache",
                        lambda path: {"tag": "v99.0.0", "url": "u",
                                      "checked_at": "2026-08-04T00:00:00+00:00"})
    body = viewer_client.get("/").get_data(as_text=True)
    assert "v99.0.0" not in body


# --- Mise à niveau depuis l'UI (oneshot systemd) --------------------------------------


def _prime_upgrade(monkeypatch, *, mode="systemd", cached_tag="v99.0.0"):
    monkeypatch.setattr("transcria.maintenance.upgrade_service.deployment_mode", lambda **_kw: mode)
    monkeypatch.setattr("transcria.maintenance.update_check.read_cache",
                        lambda path: {"tag": cached_tag, "url": "u",
                                      "checked_at": "2026-08-04T00:00:00+00:00"})


def test_upgrade_post_forbidden_for_viewer(viewer_client):
    assert viewer_client.post("/admin/maintenance/upgrade", data={}).status_code == 403


def test_upgrade_refused_in_docker(admin_client, monkeypatch):
    _prime_upgrade(monkeypatch, mode="docker")
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.upgrade_service.request_upgrade",
                        lambda **kw: triggered.setdefault("hit", True))
    resp = admin_client.post("/admin/maintenance/upgrade",
                             data={"target_tag": "v99.0.0", "acknowledge": "on"},
                             follow_redirects=True)
    assert "docker pull" in resp.get_data(as_text=True)
    assert "hit" not in triggered


def test_upgrade_requires_acknowledge(admin_client, monkeypatch):
    _prime_upgrade(monkeypatch)
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.upgrade_service.request_upgrade",
                        lambda **kw: triggered.setdefault("hit", True))
    admin_client.post("/admin/maintenance/upgrade", data={"target_tag": "v99.0.0"})
    assert "hit" not in triggered


def test_upgrade_posted_tag_must_match_verified_cache(admin_client, monkeypatch):
    # Un POST forgé ne peut pas faire déployer une ref arbitraire : seule la
    # dernière publication VÉRIFIÉE (cache) est acceptée comme cible.
    _prime_upgrade(monkeypatch, cached_tag="v99.0.0")
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.upgrade_service.request_upgrade",
                        lambda **kw: triggered.setdefault("hit", True))
    admin_client.post("/admin/maintenance/upgrade",
                      data={"target_tag": "v98.0.0", "acknowledge": "on"})
    assert "hit" not in triggered


def test_upgrade_refused_when_not_newer(admin_client, monkeypatch):
    from transcria import __version__
    _prime_upgrade(monkeypatch, cached_tag=f"v{__version__}")
    triggered: dict = {}
    monkeypatch.setattr("transcria.maintenance.upgrade_service.request_upgrade",
                        lambda **kw: triggered.setdefault("hit", True))
    admin_client.post("/admin/maintenance/upgrade",
                      data={"target_tag": f"v{__version__}", "acknowledge": "on"})
    assert "hit" not in triggered


def test_upgrade_happy_path_triggers_oneshot(admin_client, monkeypatch):
    _prime_upgrade(monkeypatch)
    called: dict = {}
    monkeypatch.setattr("transcria.maintenance.upgrade_service.request_upgrade",
                        lambda **kw: called.update(kw))
    resp = admin_client.post("/admin/maintenance/upgrade",
                             data={"target_tag": "v99.0.0", "acknowledge": "on"},
                             follow_redirects=True)
    assert resp.status_code == 200
    assert called.get("target_tag") == "v99.0.0"
    assert "lancée" in resp.get_data(as_text=True)


def test_upgrade_status_endpoint(admin_client, monkeypatch):
    from transcria import __version__
    monkeypatch.setattr("transcria.maintenance.upgrade_service.read_state",
                        lambda *a, **kw: {"status": "running", "step": 2, "steps_total": 5,
                                          "label": "Migration de la base (Alembic)"})
    data = admin_client.get("/admin/maintenance/upgrade/status").get_json()
    assert data["state"]["status"] == "running"
    assert data["current_version"] == __version__


def test_upgrade_status_forbidden_for_viewer(viewer_client):
    assert viewer_client.get("/admin/maintenance/upgrade/status").status_code == 403

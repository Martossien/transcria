"""Câblage du CLI de maintenance (vague C6, extension §3.13).

La LOGIQUE vit dans les modules testés (backup/restore/upgrade/schedule…) ;
ces tests couvrent le CÂBLAGE du CLI — dispatch, codes de sortie, messages —
en substituant les fonctions métier CHEZ LE CONSOMMATEUR (patron maison,
cf. test_maintenance_opencode_upgrade). GPU-free, sans réseau ni systemd.
"""
from __future__ import annotations

from pathlib import Path

import transcria.maintenance.cli as cli
from transcria.maintenance.backup import BackupError

_CFG = {"storage": {"jobs_dir": "./jobs", "database_url": "sqlite:///x.db"},
        "security": {"retention_days": 365, "audit_retention_days": 1095}}


def _patch_meta(monkeypatch):
    monkeypatch.setattr(cli, "_load_cfg_and_meta",
                        lambda config: (dict(_CFG), Path("/etc/transcria/config.yaml"), "9.9.9", "abc123"))


class TestBackup:
    def test_backup_creates_rotates_and_hints_verify(self, monkeypatch, tmp_path, capsys):
        _patch_meta(monkeypatch)
        archive = tmp_path / "transcria_20260716.tar.gz"
        archive.write_bytes(b"x" * 2048)
        monkeypatch.setattr(cli, "create_backup", lambda *a, **kw: archive)
        monkeypatch.setattr(cli, "rotate_backups", lambda dest, keep: ["vieux.tar.gz"])

        rc = cli.main(["backup", "--dest", str(tmp_path), "--keep", "3"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Sauvegarde créée" in out and "Rotation : 1" in out and "backup-verify" in out

    def test_backup_verify_ok_and_invalid(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(cli, "verify_backup", lambda archive: [])
        monkeypatch.setattr(cli, "read_manifest",
                            lambda archive: {"created_at": "2026-07-16", "app_version": "9.9.9",
                                             "db_kind": "postgres", "alembic_revision": "abc123"})
        assert cli.main(["backup-verify", str(tmp_path / "a.tar.gz")]) == 0
        assert "Archive saine" in capsys.readouterr().out

        monkeypatch.setattr(cli, "verify_backup", lambda archive: ["sha256 invalide"])
        assert cli.main(["backup-verify", str(tmp_path / "a.tar.gz")]) == 1
        assert "INVALIDE" in capsys.readouterr().err


class TestRestore:
    def test_dry_run_describes_without_writing(self, monkeypatch, tmp_path, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "describe_restore", lambda archive: {"app_version": "9.9.9"})
        called = {"restore": False}
        monkeypatch.setattr(cli, "restore_backup",
                            lambda *a, **kw: called.__setitem__("restore", True))
        rc = cli.main(["restore", str(tmp_path / "a.tar.gz"), "--dry-run"])
        assert rc == 0 and called["restore"] is False
        assert "À BLANC" in capsys.readouterr().out

    def test_restore_reports_and_warns_on_config_copy(self, monkeypatch, tmp_path, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "restore_backup",
                            lambda cfg, archive, force, ready_url: {
                                "restored_from": "a.tar.gz", "config_restored_as": "config.restored.yaml",
                                "app_version": "9.9.9", "db_kind": "postgres", "alembic_revision": "abc123"})
        rc = cli.main(["restore", str(tmp_path / "a.tar.gz")])
        out = capsys.readouterr().out
        assert rc == 0 and "Restauration terminée" in out and "config.restored.yaml" in out

    def test_restore_backup_error_is_exit_1(self, monkeypatch, tmp_path, capsys):
        _patch_meta(monkeypatch)

        def boom(*a, **kw):
            raise BackupError("base cible non vide (utilisez --force)")

        monkeypatch.setattr(cli, "restore_backup", boom)
        rc = cli.main(["restore", str(tmp_path / "a.tar.gz")])
        assert rc == 1 and "non vide" in capsys.readouterr().err


class TestUpgrade:
    def _steps(self):
        from types import SimpleNamespace
        return [SimpleNamespace(label="git pull", command=["git", "pull"], internal=None),
                SimpleNamespace(label="migration", command=None, internal="alembic")]

    def test_check_lists_steps_without_running(self, monkeypatch, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "build_plan", lambda **kw: self._steps())
        ran = {"n": 0}
        monkeypatch.setattr(cli, "run_plan", lambda *a, **kw: ran.__setitem__("n", ran["n"] + 1))
        rc = cli.main(["upgrade", "--check"])
        out = capsys.readouterr().out
        assert rc == 0 and ran["n"] == 0
        assert "git pull" in out and "[alembic]" in out

    def test_upgrade_runs_plan_and_shows_changelog(self, monkeypatch, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "build_plan", lambda **kw: self._steps())
        monkeypatch.setattr(cli, "run_plan", lambda steps, backup_fn, healthcheck_fn: None)
        monkeypatch.setattr(cli, "changelog_excerpt", lambda path: "- corrections diverses")
        rc = cli.main(["upgrade"])
        out = capsys.readouterr().out
        assert rc == 0 and "Mise à niveau terminée" in out and "Quoi de neuf" in out

    def test_upgrade_error_is_exit_1(self, monkeypatch, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "build_plan", lambda **kw: self._steps())

        def boom(*a, **kw):
            raise cli.UpgradeError("santé KO après redémarrage")

        monkeypatch.setattr(cli, "run_plan", boom)
        assert cli.main(["upgrade"]) == 1
        assert "santé KO" in capsys.readouterr().err


class TestInternalCommands:
    def test_model_download_forwards_args(self, monkeypatch):
        seen = {}

        def fake_download(**kw):
            seen.update(kw)
            return 0

        monkeypatch.setattr(cli, "download_from_args", fake_download)
        rc = cli.main(["model-download", "--role", "stt", "--repo", "org/model", "--kind", "hf_cache"])
        assert rc == 0
        assert seen == {"role": "stt", "repo": "org/model", "kind": "hf_cache", "file": None, "subdir": ""}

    def test_restore_apply_reports_and_fails_cleanly(self, monkeypatch, capsys):
        _patch_meta(monkeypatch)
        monkeypatch.setattr(cli, "apply_pending_restore",
                            lambda cfg, units: {"restored_from": "a.tar.gz",
                                                "app_version": "9.9.9", "db_kind": "postgres"})
        assert cli.main(["restore-apply"]) == 0
        assert "Restauration appliquée" in capsys.readouterr().out

        def boom(cfg, units):
            raise BackupError("aucune restauration en attente")

        monkeypatch.setattr(cli, "apply_pending_restore", boom)
        assert cli.main(["restore-apply"]) == 1


class TestSchedule:
    def test_status_enable_disable(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "backup_schedule_status",
                            lambda: {"unit": "transcria-backup.timer", "enabled": "enabled", "active": "waiting"})
        assert cli.main(["schedule"]) == 0
        assert "transcria-backup.timer" in capsys.readouterr().out

        monkeypatch.setattr(cli, "load_config", lambda p=None: dict(_CFG))
        monkeypatch.setattr(cli, "get_config_path", lambda p=None: Path("/etc/transcria/config.yaml"))
        from types import SimpleNamespace
        schedule = SimpleNamespace(on_calendar="daily", keep=7, service_user="transcria")
        monkeypatch.setattr(cli.BackupSchedule, "from_config",
                            classmethod(lambda cls, cfg, path, service_user=None: schedule))
        monkeypatch.setattr(cli, "install_backup_schedule", lambda s: ["unit écrite", "timer activé"])
        assert cli.main(["schedule", "--enable"]) == 0
        assert "activé" in capsys.readouterr().out

        monkeypatch.setattr(cli, "remove_backup_schedule", lambda: ["timer retiré"])
        assert cli.main(["schedule", "--disable"]) == 0
        assert "désactivé" in capsys.readouterr().out

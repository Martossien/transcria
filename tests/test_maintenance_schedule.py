"""Backup planifié : rendu des unités systemd, install/remove, résolution config, schéma."""
from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config
from transcria.maintenance.schedule import (
    SERVICE_UNIT,
    TIMER_UNIT,
    BackupSchedule,
    PurgeSchedule,
    backup_schedule_status,
    install_backup_schedule,
    install_purge_schedule,
    remove_backup_schedule,
    remove_purge_schedule,
)


def _sched(**overrides) -> BackupSchedule:
    base = dict(
        install_dir="/opt/transcria", service_user="svc",
        python_bin="/opt/transcria/venv/bin/python", config_path="/etc/transcria/config.yaml",
        env_file="/opt/transcria/.env", backup_dir="/var/backups/transcria",
        on_calendar="*-*-* 03:00:00", keep=14, exclude_audio=True,
    )
    base.update(overrides)
    return BackupSchedule(**base)


def test_render_service_exec_user_workdir():
    txt = _sched().render_service()
    assert "Type=oneshot" in txt and "User=svc" in txt
    assert "WorkingDirectory=/opt/transcria" in txt
    assert "EnvironmentFile=/opt/transcria/.env" in txt
    assert ("ExecStart=/opt/transcria/venv/bin/python -m transcria.maintenance.cli "
            "--config /etc/transcria/config.yaml backup --dest /var/backups/transcria "
            "--keep 14 --exclude-audio") in txt


def test_render_service_omits_exclude_audio_when_false():
    assert "--exclude-audio" not in _sched(exclude_audio=False).render_service()


def test_render_timer_oncalendar_persistent():
    txt = _sched().render_timer()
    assert "OnCalendar=*-*-* 03:00:00" in txt
    assert "Persistent=true" in txt
    assert "WantedBy=timers.target" in txt


def test_from_config_reads_section():
    cfg = {"maintenance": {"backup_dir": "/b",
                           "schedule": {"on_calendar": "weekly", "keep": 3, "exclude_audio": True}}}
    s = BackupSchedule.from_config(cfg, "/cfg.yaml", install_dir="/opt/x", service_user="u", python_bin="/py")
    assert (s.backup_dir, s.on_calendar, s.keep, s.exclude_audio) == ("/b", "weekly", 3, True)
    assert s.config_path == "/cfg.yaml" and s.env_file == "/opt/x/.env"


def test_from_config_uses_defaults_when_absent():
    s = BackupSchedule.from_config({}, "/cfg.yaml", install_dir="/opt/x", service_user="u", python_bin="/py")
    assert (s.backup_dir, s.on_calendar, s.keep, s.exclude_audio) == ("./backups", "*-*-* 02:00:00", 7, False)


def test_install_writes_units_and_enables_timer(tmp_path: Path):
    written: dict = {}
    calls: list = []

    def write(path: Path, content: str) -> None:
        written[path.name] = content

    def run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    install_backup_schedule(_sched(), units_dir=tmp_path, run=run, write=write)
    assert SERVICE_UNIT in written and TIMER_UNIT in written
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", TIMER_UNIT] in calls


def test_remove_disables_and_deletes_units(tmp_path: Path):
    (tmp_path / SERVICE_UNIT).write_text("x")
    (tmp_path / TIMER_UNIT).write_text("y")
    calls: list = []

    def run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    remove_backup_schedule(units_dir=tmp_path, run=run)
    assert not (tmp_path / SERVICE_UNIT).exists()
    assert not (tmp_path / TIMER_UNIT).exists()
    assert ["systemctl", "disable", "--now", TIMER_UNIT] in calls


def test_resolve_service_user_from_systemctl():
    from transcria.maintenance.schedule import resolve_service_user

    def run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="root\n", stderr="")

    assert resolve_service_user(run=run) == "root"


def test_resolve_service_user_defaults_root_when_empty():
    from transcria.maintenance.schedule import resolve_service_user

    def run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="\n", stderr="")

    assert resolve_service_user(run=run) == "root"


def test_status_reads_systemctl():
    def run(cmd, **_kw):
        value = "enabled" if "is-enabled" in cmd else "active"
        return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")

    status = backup_schedule_status(run=run)
    assert status["enabled"] == "enabled" and status["active"] == "active"


def test_schema_accepts_default_maintenance_section():
    assert validate_config(get_default_config()).is_valid


def test_schema_rejects_bad_maintenance_types():
    cfg = get_default_config()
    cfg["maintenance"]["schedule"]["keep"] = -1
    cfg["maintenance"]["schedule"]["enabled"] = "yes"
    result = validate_config(cfg)
    joined = " ".join(result.errors)
    assert "maintenance.schedule.keep" in joined and "maintenance.schedule.enabled" in joined


# ── Timer de purge (PISTES_AMELIORATION §6.2) ────────────────────────────────

def _purge_sched(**overrides) -> PurgeSchedule:
    base = dict(
        install_dir="/opt/transcria",
        service_user="transcria",
        python_bin="/opt/transcria/venv/bin/python",
        config_path="/opt/transcria/config.yaml",
        env_file="/opt/transcria/.env",
        on_calendar="*-*-* 03:30:00",
    )
    base.update(overrides)
    return PurgeSchedule(**base)


class TestPurgeSchedule:
    def test_render_service_lance_la_cli_purge(self):
        text = _purge_sched().render_service()
        assert "ExecStart=/opt/transcria/venv/bin/python -m transcria.maintenance.cli " \
               "--config /opt/transcria/config.yaml purge" in text
        assert "User=transcria" in text
        assert "Type=oneshot" in text

    def test_render_timer_oncalendar(self):
        text = _purge_sched(on_calendar="*-*-* 04:00:00").render_timer()
        assert "OnCalendar=*-*-* 04:00:00" in text
        assert "Persistent=true" in text

    def test_from_config_lit_purge_on_calendar(self):
        cfg = {"maintenance": {"schedule": {"purge_on_calendar": "Sun *-*-* 05:00:00"}}}
        sched = PurgeSchedule.from_config(cfg, "/x/config.yaml", install_dir="/x",
                                          service_user="svc", python_bin="/x/venv/bin/python")
        assert sched.on_calendar == "Sun *-*-* 05:00:00"

    def test_from_config_defaut_apres_le_backup(self):
        # Décalé APRÈS la sauvegarde de 02:00 : on n'efface qu'une fois l'archive du jour produite.
        sched = PurgeSchedule.from_config({}, "/x/config.yaml", install_dir="/x",
                                          service_user="svc", python_bin="/x/venv/bin/python")
        assert sched.on_calendar == "*-*-* 03:30:00"

    def test_install_ecrit_les_unites_purge(self, tmp_path: Path):
        calls = []
        written = {}
        actions = install_purge_schedule(
            _purge_sched(),
            units_dir=tmp_path,
            run=lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
            write=lambda path, content: written.update({path.name: content}),
        )
        assert "transcria-purge.service" in written
        assert "transcria-purge.timer" in written
        assert ["systemctl", "enable", "--now", "transcria-purge.timer"] in calls
        assert any("transcria-purge.timer" in a for a in actions)

    def test_remove_supprime_les_unites_purge(self, tmp_path: Path):
        (tmp_path / "transcria-purge.service").write_text("x")
        (tmp_path / "transcria-purge.timer").write_text("x")
        calls = []
        actions = remove_purge_schedule(
            units_dir=tmp_path,
            run=lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        assert not (tmp_path / "transcria-purge.service").exists()
        assert not (tmp_path / "transcria-purge.timer").exists()
        assert ["systemctl", "disable", "--now", "transcria-purge.timer"] in calls
        assert any("supprimé" in a for a in actions)

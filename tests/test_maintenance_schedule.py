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
    backup_schedule_status,
    install_backup_schedule,
    remove_backup_schedule,
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

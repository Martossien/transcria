"""Backup planifié via un timer systemd (C1.3).

Génère et installe deux unités : ``transcria-backup.service`` (oneshot qui lance la CLI
``maintenance backup`` déjà testée) et ``transcria-backup.timer`` (déclencheur ``OnCalendar``).
La génération est **pure** (testable sans systemd) ; l'installation passe par un ``run`` et un
``writer`` injectables. Aucune logique de sauvegarde ici — seulement l'ordonnancement.

Choix : `Persistent=true` (une sauvegarde manquée pendant une coupure est rattrapée au boot) ;
l'unité oneshot lit la config via ``--config`` explicite + `EnvironmentFile` (`.env` → DSN base).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SERVICE_UNIT = "transcria-backup.service"
TIMER_UNIT = "transcria-backup.timer"
DEFAULT_UNITS_DIR = Path("/etc/systemd/system")

RunFn = Callable[..., subprocess.CompletedProcess]
WriteFn = Callable[[Path, str], None]


def _default_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@dataclass(frozen=True)
class BackupSchedule:
    """Tous les paramètres nécessaires au rendu des unités (résolus une fois)."""
    install_dir: str
    service_user: str
    python_bin: str
    config_path: str
    env_file: str
    backup_dir: str
    on_calendar: str
    keep: int
    exclude_audio: bool

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        config_path: str,
        *,
        install_dir: str | None = None,
        service_user: str | None = None,
        python_bin: str | None = None,
    ) -> "BackupSchedule":
        maint = cfg.get("maintenance", {}) or {}
        sched = maint.get("schedule", {}) or {}
        resolved_install = install_dir or str(Path(__file__).resolve().parents[2])
        return cls(
            install_dir=resolved_install,
            # Le backup doit tourner comme le SERVICE PRINCIPAL (il possède jobs/ ; un service root
            # crée des fichiers root que le propriétaire du dossier d'install ne peut PAS lire).
            service_user=service_user or resolve_service_user(),
            python_bin=python_bin or sys.executable,
            config_path=str(config_path),
            env_file=str(Path(resolved_install) / ".env"),
            backup_dir=str(maint.get("backup_dir") or "./backups"),
            on_calendar=str(sched.get("on_calendar") or "*-*-* 02:00:00"),
            keep=int(sched.get("keep") or 7),
            exclude_audio=bool(sched.get("exclude_audio", False)),
        )

    def exec_start(self) -> str:
        parts = [
            self.python_bin, "-m", "transcria.maintenance.cli",
            "--config", self.config_path,
            "backup", "--dest", self.backup_dir, "--keep", str(self.keep),
        ]
        if self.exclude_audio:
            parts.append("--exclude-audio")
        return " ".join(parts)

    def render_service(self) -> str:
        return (
            "[Unit]\n"
            "Description=TranscrIA — sauvegarde planifiée (oneshot)\n"
            "After=network-online.target postgresql.service\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"User={self.service_user}\n"
            f"WorkingDirectory={self.install_dir}\n"
            f"EnvironmentFile={self.env_file}\n"
            f"ExecStart={self.exec_start()}\n"
        )

    def render_timer(self) -> str:
        return (
            "[Unit]\n"
            "Description=TranscrIA — planification des sauvegardes\n\n"
            "[Timer]\n"
            f"OnCalendar={self.on_calendar}\n"
            "Persistent=true\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )


def resolve_service_user(unit: str = "transcria.service", *, run: RunFn = subprocess.run) -> str:
    """Utilisateur du service principal (il possède les données à sauvegarder). Défaut : root.

    root peut TOUT lire ; un service non-root crée des données lisibles par lui — dans les deux
    cas, faire tourner le backup sous CET utilisateur évite les `PermissionError` sur jobs/."""
    try:
        result = run(["systemctl", "show", unit, "-p", "User", "--value"],
                     capture_output=True, text=True, check=False)
        user = (getattr(result, "stdout", "") or "").strip()
        return user or "root"
    except OSError:
        return "root"


def install_backup_schedule(
    schedule: BackupSchedule,
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> list[str]:
    """Écrit les deux unités, recharge systemd et active le timer. Retourne les actions faites."""
    actions: list[str] = []
    write(units_dir / SERVICE_UNIT, schedule.render_service())
    actions.append(f"écrit {SERVICE_UNIT}")
    write(units_dir / TIMER_UNIT, schedule.render_timer())
    actions.append(f"écrit {TIMER_UNIT}")
    run(["systemctl", "daemon-reload"], check=True)
    actions.append("daemon-reload")
    run(["systemctl", "enable", "--now", TIMER_UNIT], check=True)
    actions.append(f"enable --now {TIMER_UNIT}")
    return actions


def remove_backup_schedule(
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
) -> list[str]:
    """Désactive le timer et supprime les deux unités. Best-effort, idempotent."""
    actions: list[str] = []
    run(["systemctl", "disable", "--now", TIMER_UNIT], check=False)
    actions.append(f"disable --now {TIMER_UNIT}")
    for unit in (TIMER_UNIT, SERVICE_UNIT):
        path = units_dir / unit
        if path.exists():
            path.unlink()
            actions.append(f"supprimé {unit}")
    run(["systemctl", "daemon-reload"], check=False)
    actions.append("daemon-reload")
    return actions


def backup_schedule_status(*, run: RunFn = subprocess.run) -> dict:
    """État du timer : actif ? prochaine échéance (via `systemctl list-timers`)."""
    result = run(["systemctl", "is-enabled", TIMER_UNIT], capture_output=True, text=True, check=False)
    enabled = (getattr(result, "stdout", "") or "").strip()
    active = run(["systemctl", "is-active", TIMER_UNIT], capture_output=True, text=True, check=False)
    return {
        "unit": TIMER_UNIT,
        "enabled": enabled,
        "active": (getattr(active, "stdout", "") or "").strip(),
    }

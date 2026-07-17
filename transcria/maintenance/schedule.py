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
PURGE_SERVICE_UNIT = "transcria-purge.service"
PURGE_TIMER_UNIT = "transcria-purge.timer"
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


@dataclass(frozen=True)
class PurgeSchedule:
    """Purge de rétention planifiée — même patron que BackupSchedule (rendu pur, testable).

    Sans timer, la purge ne tourne qu'au chargement de la page d'accueil ou à la main :
    une instance sans visite laisse jobs/ croître sans borne (voir PISTES_AMELIORATION §6.2).
    """
    install_dir: str
    service_user: str
    python_bin: str
    config_path: str
    env_file: str
    on_calendar: str

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        config_path: str,
        *,
        install_dir: str | None = None,
        service_user: str | None = None,
        python_bin: str | None = None,
    ) -> "PurgeSchedule":
        maint = cfg.get("maintenance", {}) or {}
        sched = maint.get("schedule", {}) or {}
        resolved_install = install_dir or str(Path(__file__).resolve().parents[2])
        return cls(
            install_dir=resolved_install,
            service_user=service_user or resolve_service_user(),
            python_bin=python_bin or sys.executable,
            config_path=str(config_path),
            env_file=str(Path(resolved_install) / ".env"),
            # Décalé par défaut après la sauvegarde de 02:00 : on n'efface qu'APRÈS
            # avoir une archive du jour.
            on_calendar=str(sched.get("purge_on_calendar") or "*-*-* 03:30:00"),
        )

    def exec_start(self) -> str:
        return " ".join([
            self.python_bin, "-m", "transcria.maintenance.cli",
            "--config", self.config_path, "purge",
        ])

    def render_service(self) -> str:
        return (
            "[Unit]\n"
            "Description=TranscrIA — purge de rétention planifiée (oneshot)\n"
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
            "Description=TranscrIA — planification de la purge de rétention\n\n"
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


def _install_units(
    service_unit: str,
    timer_unit: str,
    service_text: str,
    timer_text: str,
    *,
    units_dir: Path,
    run: RunFn,
    write: WriteFn,
) -> list[str]:
    actions: list[str] = []
    write(units_dir / service_unit, service_text)
    actions.append(f"écrit {service_unit}")
    write(units_dir / timer_unit, timer_text)
    actions.append(f"écrit {timer_unit}")
    run(["systemctl", "daemon-reload"], check=True)
    actions.append("daemon-reload")
    run(["systemctl", "enable", "--now", timer_unit], check=True)
    actions.append(f"enable --now {timer_unit}")
    return actions


def _remove_units(
    service_unit: str,
    timer_unit: str,
    *,
    units_dir: Path,
    run: RunFn,
) -> list[str]:
    actions: list[str] = []
    run(["systemctl", "disable", "--now", timer_unit], check=False)
    actions.append(f"disable --now {timer_unit}")
    for unit in (timer_unit, service_unit):
        path = units_dir / unit
        if path.exists():
            path.unlink()
            actions.append(f"supprimé {unit}")
    run(["systemctl", "daemon-reload"], check=False)
    actions.append("daemon-reload")
    return actions


def _timer_status(timer_unit: str, *, run: RunFn) -> dict:
    result = run(["systemctl", "is-enabled", timer_unit], capture_output=True, text=True, check=False)
    enabled = (getattr(result, "stdout", "") or "").strip()
    active = run(["systemctl", "is-active", timer_unit], capture_output=True, text=True, check=False)
    return {
        "unit": timer_unit,
        "enabled": enabled,
        "active": (getattr(active, "stdout", "") or "").strip(),
    }


def install_backup_schedule(
    schedule: BackupSchedule,
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> list[str]:
    """Écrit les deux unités, recharge systemd et active le timer. Retourne les actions faites."""
    return _install_units(SERVICE_UNIT, TIMER_UNIT, schedule.render_service(), schedule.render_timer(),
                          units_dir=units_dir, run=run, write=write)


def remove_backup_schedule(
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
) -> list[str]:
    """Désactive le timer et supprime les deux unités. Best-effort, idempotent."""
    return _remove_units(SERVICE_UNIT, TIMER_UNIT, units_dir=units_dir, run=run)


def backup_schedule_status(*, run: RunFn = subprocess.run) -> dict:
    """État du timer : actif ? prochaine échéance (via `systemctl list-timers`)."""
    return _timer_status(TIMER_UNIT, run=run)


def install_purge_schedule(
    schedule: PurgeSchedule,
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> list[str]:
    """Installe le timer de purge de rétention (mêmes mécanique et injections que le backup)."""
    return _install_units(PURGE_SERVICE_UNIT, PURGE_TIMER_UNIT,
                          schedule.render_service(), schedule.render_timer(),
                          units_dir=units_dir, run=run, write=write)


def remove_purge_schedule(
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
) -> list[str]:
    """Désactive le timer de purge et supprime ses unités. Best-effort, idempotent."""
    return _remove_units(PURGE_SERVICE_UNIT, PURGE_TIMER_UNIT, units_dir=units_dir, run=run)


def purge_schedule_status(*, run: RunFn = subprocess.run) -> dict:
    """État du timer de purge."""
    return _timer_status(PURGE_TIMER_UNIT, run=run)

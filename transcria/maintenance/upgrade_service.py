"""Mise à niveau depuis l'UI via un one-shot systemd PRIVILÉGIÉ (patron restore_service).

Problème : le worker web ne peut pas se mettre à niveau lui-même — un sous-process
lancé par le worker vit dans le cgroup de ``transcria.service`` et le ``systemctl
restart`` du plan le tuerait en plein vol (mise à niveau à moitié faite). Solution :
l'UI **dépose une demande** (tag cible) puis déclenche l'unité oneshot
``transcria-upgrade.service`` (``User=root``, HORS cgroup) qui exécute le plan
outillé EXISTANT (sauvegarde → checkout du tag → migration Alembic → restart →
``/ready``) en journalisant sa progression dans un fichier d'état que la page sonde.

Le web ne fait qu'un ``systemctl start --no-block`` (retour immédiat) ; pendant le
restart, la page affiche « redémarrage… » et se raccroche dès que ``/ready`` répond.
Tout est injectable (``run``/``write``/chemins) pour être testé sans systemd.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from transcria.maintenance.restore_service import RunFn, WriteFn, _default_write
from transcria.maintenance.upgrade import UpgradeError, build_plan, run_plan

UPGRADE_UNIT = "transcria-upgrade.service"
REQUEST_PATH = Path("/run/transcria-upgrade.request")
STATE_PATH = Path("/run/transcria-upgrade.state.json")
DEFAULT_UNITS = "transcria.service"
DEFAULT_UNITS_DIR = Path("/etc/systemd/system")

# Strictement un tag de version (v0.5.0, 0.3.9.1) : cette valeur part dans un
# ``git checkout`` exécuté en root — pas de branche, pas de ref arbitraire.
_TAG_RE = re.compile(r"^v?\d+(\.\d+){1,3}$")


def valid_target_tag(tag: str) -> bool:
    return bool(_TAG_RE.match(tag or ""))


def deployment_mode(
    *,
    dockerenv: Path = Path("/.dockerenv"),
    systemd_dir: Path = Path("/run/systemd/system"),
) -> str:
    """``docker`` (image immuable → instructions ``docker pull``), ``systemd``
    (bare-metal outillé → bouton), ou ``unsupported`` (ni l'un ni l'autre → CLI)."""
    if dockerenv.exists():
        return "docker"
    if systemd_dir.is_dir():
        return "systemd"
    return "unsupported"


def render_upgrade_unit(*, install_dir: str, python_bin: str, config_path: str,
                        env_file: str, units: str) -> str:
    """Unité oneshot privilégiée — même contrat que la restauration : PAS de section
    [Install] (déclenchée à la demande, jamais activée au boot)."""
    return (
        "[Unit]\n"
        "Description=TranscrIA — mise à niveau outillée (oneshot privilégié)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        f"WorkingDirectory={install_dir}\n"
        f"EnvironmentFile={env_file}\n"
        f"ExecStart={python_bin} -m transcria.maintenance.cli --config {config_path} "
        f"upgrade-apply --units {units}\n"
    )


def ensure_upgrade_unit(
    unit_text: str,
    *,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> bool:
    """Écrit l'unité si absente/différente et recharge systemd. Retourne True si (ré)écrite."""
    path = units_dir / UPGRADE_UNIT
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == unit_text:
        return False
    write(path, unit_text)
    run(["systemctl", "daemon-reload"], check=True)
    return True


def _write_state(state: dict, path: Path, write: WriteFn) -> None:
    state = {**state, "updated_at": datetime.now(UTC).isoformat()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path, json.dumps(state, ensure_ascii=False))
    except OSError:
        pass  # un état inécrivable ne doit jamais interrompre la mise à niveau


def read_state(state_path: Path = STATE_PATH) -> dict | None:
    """État de la mise à niveau en cours/passée, ou ``None`` (jamais d'exception)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("status") else None


def request_upgrade(
    *,
    install_dir: str,
    python_bin: str,
    config_path: str,
    env_file: str,
    target_tag: str,
    units: str = DEFAULT_UNITS,
    request_path: Path = REQUEST_PATH,
    state_path: Path = STATE_PATH,
    units_dir: Path = DEFAULT_UNITS_DIR,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
) -> None:
    """Prépare l'unité, initialise l'état, dépose la demande et déclenche le oneshot.

    Non bloquant : le service courant continue de répondre jusqu'à l'étape restart
    du plan (exécutée par le oneshot, hors du cgroup du service)."""
    if not valid_target_tag(target_tag):
        raise ValueError(f"tag cible invalide : {target_tag!r} (attendu : v0.5.0, 0.3.9.1…)")
    unit_text = render_upgrade_unit(install_dir=install_dir, python_bin=python_bin,
                                    config_path=config_path, env_file=env_file, units=units)
    ensure_upgrade_unit(unit_text, units_dir=units_dir, run=run, write=write)
    _write_state({"status": "requested", "target": target_tag}, state_path, write)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    write(request_path, json.dumps({"target_ref": target_tag}) + "\n")
    run(["systemctl", "start", "--no-block", UPGRADE_UNIT], check=True)


def apply_pending_upgrade(
    *,
    backup_fn,
    healthcheck_fn,
    units: str = DEFAULT_UNITS,
    ready_url: str = "http://127.0.0.1:7870/ready",
    request_path: Path = REQUEST_PATH,
    state_path: Path = STATE_PATH,
    run: RunFn = subprocess.run,
    write: WriteFn = _default_write,
    echo=print,
) -> dict:
    """Applique la mise à niveau en attente en journalisant chaque étape dans l'état.

    Demande consommée AVANT exécution (jamais de re-déclenchement en boucle d'une
    demande qui échoue). Lève ``UpgradeError`` si rien en attente / tag invalide /
    étape en échec — l'état ``failed`` porte alors le message pour la page."""
    if not request_path.exists():
        raise UpgradeError("aucune demande de mise à niveau en attente")
    try:
        target = str(json.loads(request_path.read_text(encoding="utf-8")).get("target_ref") or "")
    except ValueError:
        target = ""
    request_path.unlink(missing_ok=True)
    if not valid_target_tag(target):
        _write_state({"status": "failed", "target": target,
                      "error": f"tag cible invalide : {target!r}"}, state_path, write)
        raise UpgradeError(f"tag cible invalide dans la demande : {target!r}")

    # repo_dir = CWD : l'unité oneshot fixe WorkingDirectory=<install_dir> — le grant
    # safe.directory doit viser CE dépôt (git en root sur un clone d'opérateur).
    steps = build_plan(target_ref=target, do_pull=False,
                       restart_units=[u for u in units.split(",") if u], ready_url=ready_url,
                       repo_dir=os.getcwd())
    log: list[str] = []
    state: dict = {"status": "running", "target": target,
                   "steps_total": len(steps), "step": 0, "label": "", "log": log}
    _write_state(state, state_path, write)

    def on_step(i: int, total: int, label: str) -> None:
        state.update(step=i, steps_total=total, label=label)
        log.append(label)
        _write_state(state, state_path, write)

    try:
        report = run_plan(steps, backup_fn=backup_fn, healthcheck_fn=healthcheck_fn,
                          runner=run, echo=echo, on_step=on_step)
    except UpgradeError as exc:
        _write_state({**state, "status": "failed", "error": str(exc)}, state_path, write)
        raise
    _write_state({**state, "status": "ok", "label": ""}, state_path, write)
    return report

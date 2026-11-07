"""Redémarrage du service depuis l'interface — one-shot systemd privilégié (patron de la restauration).

Le process web ne peut pas se redémarrer lui-même proprement : un ``systemctl restart`` lancé
depuis le worker le tuerait à mi-chemin. On dépose donc une unité ``oneshot`` ``User=root`` sans
section ``[Install]`` (jamais au boot), déclenchée ``--no-block`` : elle redémarre
``transcria.service`` une fois la réponse HTTP rendue. Si l'écriture de l'unité ou ``systemctl``
échoue (service hors systemd, droits), l'appelant affiche la commande manuelle.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

RESTART_UNIT = "transcria-restart.service"
DEFAULT_UNITS = "transcria.service"
DEFAULT_UNITS_DIR = Path("/etc/systemd/system")

RunFn = Callable[..., subprocess.CompletedProcess]
WriteFn = Callable[[Path, str], None]


def _default_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def render_restart_unit(units: str = DEFAULT_UNITS) -> str:
    return (
        "[Unit]\n"
        "Description=TranscrIA — redémarrage du service demandé depuis l'administration (oneshot privilégié)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        f"ExecStart=/bin/systemctl restart {units}\n"
    )


def request_restart(*, units: str = DEFAULT_UNITS, units_dir: Path = DEFAULT_UNITS_DIR,
                    run: RunFn = subprocess.run, write: WriteFn = _default_write) -> None:
    """Écrit l'unité si absente/différente, recharge systemd, déclenche le redémarrage (non bloquant).

    Raises:
        OSError / subprocess.CalledProcessError : l'appelant montre la commande manuelle.
    """
    path = units_dir / RESTART_UNIT
    unit_text = render_restart_unit(units)
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current != unit_text:
        write(path, unit_text)
        run(["systemctl", "daemon-reload"], check=True)
    run(["systemctl", "start", "--no-block", RESTART_UNIT], check=True)

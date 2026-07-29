"""Phase d'installation « connectors » — meeting-runner : dépendances, config, unité systemd.

Vague 4 du plan UI_REUNIONS (§8) : jusqu'ici, RIEN de ce chantier n'était atteignable par
quelqu'un qui installe TranscrIA (`requirements-connectors.txt` annonçait « la phase
connecteur de l'installeur » — elle n'existait pas). Cette phase, opt-in :

1. installe les dépendances connecteurs dans le venv du projet ;
2. génère un squelette de configuration runner (`runner.yaml`, chemin du jeton à compléter) ;
3. installe l'unité systemd `transcria-meeting-runner.service` (si demandé, root requis).

Patron des phases (docs/archive/PLAN_EVOLUTION_INSTALLATION.md) : plan gelé, runner injecté,
erreurs typées, idempotence par marqueurs — `install.sh` délègue, la CLI expose.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Runner(Protocol):
    def __call__(self, cmd: list[str]) -> None: ...


class ConnectorsPhaseError(RuntimeError):
    """Échec actionnable de la phase connecteurs (message sans secret)."""


@dataclass(frozen=True)
class ConnectorsPlan:
    repo_root: Path
    venv_python: Path
    config_dir: Path                      # où poser runner.yaml (défaut : racine du dépôt)
    install_systemd: bool = False
    systemd_dir: Path = Path("/etc/systemd/system")


RUNNER_CONFIG_TEMPLATE = """\
# Configuration du meeting-runner (vague 4 — docs/UI_REUNIONS_WORKFLOW.md).
# Démarrage : TRANSCRIA_RUNNER_CONFIG={config_path} python -m connector_service.runner
portal_url: http://127.0.0.1:7870
# Jeton d'API d'un compte listé dans connectors.meetings.runner_usernames (portail).
# Générer : venv/bin/python -m transcria.maintenance.cli create-runner-token svc-runner
token_file: {config_dir}/runner_token.txt
runner_name: meeting-runner-1
capacity: 2
poll_interval_s: 30
platforms: [jitsi]
# images:                     # digests GHCR épinglés — défauts : images locales bot.sh
#   jitsi: ghcr.io/<owner>/transcria-bot@sha256:…
"""

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=TranscrIA — meeting-runner (bots de réunion planifiés)
Documentation=file://{repo_root}/docs/UI_REUNIONS_WORKFLOW.md
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
Environment=TRANSCRIA_RUNNER_CONFIG={config_path}
ExecStart={venv_python} -m connector_service.runner
Restart=on-failure
RestartSec=10
# Un bot en réunion n'est jamais coupé par un simple redéploiement : le démon attend ses
# sessions à l'arrêt ; cette borne couvre le pire cas (réunion de 4 h max côté bot).
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
"""


def connectors_deps_complete(venv_python: Path) -> bool:
    """Idempotence : la dépendance MARQUEUR des connecteurs (PyJWT) est-elle importable ?"""
    import subprocess

    probe = subprocess.run([str(venv_python), "-c", "import jwt"], capture_output=True)
    return probe.returncode == 0


def apply_connectors(plan: ConnectorsPlan, *, console, runner: Runner) -> None:
    requirements = plan.repo_root / "requirements-connectors.txt"
    if not requirements.exists():
        raise ConnectorsPhaseError(f"requirements-connectors.txt introuvable ({requirements})")
    if connectors_deps_complete(plan.venv_python):
        console.ok("dépendances connecteurs déjà présentes")
    else:
        console.info("installation des dépendances connecteurs (opt-in)…")
        runner([str(plan.venv_python), "-m", "pip", "install", "-q", "-r", str(requirements)])

    config_path = plan.config_dir / "runner.yaml"
    if config_path.exists():
        console.ok(f"config runner déjà présente : {config_path}")
    else:
        config_path.write_text(
            RUNNER_CONFIG_TEMPLATE.format(config_path=config_path, config_dir=plan.config_dir),
            encoding="utf-8")
        console.ok(f"squelette de config runner écrit : {config_path} (compléter token_file)")

    if plan.install_systemd:
        unit_path = plan.systemd_dir / "transcria-meeting-runner.service"
        unit_path.write_text(
            SYSTEMD_UNIT_TEMPLATE.format(repo_root=plan.repo_root,
                                         config_path=config_path,
                                         venv_python=plan.venv_python),
            encoding="utf-8")
        runner(["systemctl", "daemon-reload"])
        console.ok(f"unité systemd installée : {unit_path} (activer : systemctl enable --now "
                   "transcria-meeting-runner)")

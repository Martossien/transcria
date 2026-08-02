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
    install_browsers: bool = False        # playwright install chromium (bot HORS conteneur)
    install_deps: bool = True             # False = ne poser que config + unité (flux TOUJOURS)
    env_file: Path | None = None          # .env où poser la clé de chiffrement (défaut repo)


RUNNER_CONFIG_TEMPLATE = """\
# Configuration du meeting-runner (vague 4 — docs/archive/UI_REUNIONS_WORKFLOW.md).
# Démarrage : TRANSCRIA_RUNNER_CONFIG={config_path} python -m connector_service.runner
portal_url: http://127.0.0.1:7870
# Jeton d'exécutant : DÉPOSÉ AUTOMATIQUEMENT par le bouton « Activer » de
# /admin/connecteurs (auto-provisionnement) — rien à faire ici dans le cas nominal.
token_file: {config_dir}/instance/{token_filename}
runner_name: meeting-runner-1
capacity: 2
poll_interval_s: 30
platforms: [jitsi]
# Autres plateformes (docs/VISIO_ZOOM_RUNNER.md) — ajouter l'id ET poser les identités
# machine dans l'environnement du runner :
#   visio     → LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET (exploitant)
#   zoom-sdk  → ZOOM_CLIENT_ID + ZOOM_CLIENT_SECRET (app Meeting SDK ; gratuit = 40 min)
# platforms: [jitsi, visio, zoom-sdk]
# images:                     # digests GHCR épinglés — défauts : images locales bot.sh
#   jitsi: ghcr.io/<owner>/transcria-bot@sha256:…
"""

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=TranscrIA — meeting-runner (bots de réunion planifiés)
Documentation=file://{repo_root}/docs/archive/UI_REUNIONS_WORKFLOW.md
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_root}
Environment=PYTHONPATH={repo_root}
Environment=TRANSCRIA_RUNNER_CONFIG={config_path}
# Environnement MACHINE relayé aux conteneurs de bots (`_MACHINE_ENV` de
# `runner/commands.py`) : VISIO_ALLOWED_HOSTS, VISIO_API_BASE, BOT_HIDDEN, identités
# LiveKit/Jitsi… systemd ne peuple PAS cet environnement tout seul — sans cette ligne, ces
# variables sont posées quelque part et n'atteignent jamais le runner, donc jamais le bot.
# Le « - » rend le fichier facultatif : son absence ne bloque pas le démarrage.
EnvironmentFile=-{repo_root}/.env
ExecStart={venv_python} -m connector_service.runner
Restart=on-failure
RestartSec=10
# Un bot en réunion n'est jamais coupé par un simple redéploiement : le démon attend ses
# sessions à l'arrêt ; cette borne couvre le pire cas (réunion de 4 h max côté bot).
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
"""

# Unité du SERVICE MEET — distincte du meeting-runner, et volontairement : le runner fait
# entrer des bots dans des réunions en cours, celui-ci récupère des enregistrements APRÈS
# coup. Deux responsabilités, deux cycles de vie, deux pannes à diagnostiquer séparément.
# Elle est DORMANTE tant que la fiche Meet n'est pas renseignée : le service dort et le dit,
# au lieu d'entrer en boucle de redémarrage (cf. `connector_service/meet_service.py`).
MEET_UNIT_TEMPLATE = """\
[Unit]
Description=TranscrIA — service Meet (ingestion post-réunion depuis Google Workspace)
Documentation=file://{repo_root}/docs/MEET_TEAMS_ADMIN.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_root}
Environment=PYTHONPATH={repo_root}
Environment=TRANSCRIA_REPO_ROOT={repo_root}
EnvironmentFile=-{repo_root}/.env
ExecStart={venv_python} -m connector_service.meet_main
Restart=on-failure
RestartSec=30
# Un téléchargement d'enregistrement puis son téléversement au portail peuvent être longs :
# on laisse le tour en cours se terminer plutôt que de le couper au redéploiement.
TimeoutStopSec=120

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
    if not plan.install_deps:
        console.ok("dépendances connecteurs : ignorées (pose du runner dormant seulement)")
    elif connectors_deps_complete(plan.venv_python):
        console.ok("dépendances connecteurs déjà présentes")
    else:
        console.info("installation des dépendances connecteurs (opt-in)…")
        runner([str(plan.venv_python), "-m", "pip", "install", "-q", "-r", str(requirements)])

    config_path = plan.config_dir / "runner.yaml"
    if config_path.exists():
        console.ok(f"config runner déjà présente : {config_path}")
    else:
        config_path.write_text(
            RUNNER_CONFIG_TEMPLATE.format(config_path=config_path, config_dir=plan.config_dir, token_filename="meeting_runner_token.txt"),
            encoding="utf-8")
        console.ok(f"squelette de config runner écrit : {config_path} (compléter token_file)")

    _ensure_meeting_ref_key(plan, console)

    if plan.install_browsers:
        console.info("navigateurs Playwright (bot hors conteneur)…")
        runner([str(plan.venv_python), "-m", "playwright", "install", "chromium"])

    if plan.install_systemd:
        unit_path = plan.systemd_dir / "transcria-meeting-runner.service"
        try:
            unit_path.write_text(
                SYSTEMD_UNIT_TEMPLATE.format(repo_root=plan.repo_root,
                                             config_path=config_path,
                                             venv_python=plan.venv_python),
                encoding="utf-8")
        except PermissionError:
            # Jamais bloquant : la check-list admin affiche le remède exact.
            console.info(f"droits insuffisants pour {unit_path} — poser l'unité en root : "
                         "sudo venv/bin/python -m transcria.installer.cli connectors --no-deps --systemd")
            return
        meet_unit_path = plan.systemd_dir / "transcria-meet-poller.service"
        meet_unit_path.write_text(
            MEET_UNIT_TEMPLATE.format(repo_root=plan.repo_root,
                                      venv_python=plan.venv_python),
            encoding="utf-8")
        runner(["systemctl", "daemon-reload"])
        # DORMANTES par défaut (décision utilisateur) : démarrées tout de suite, elles
        # patientent tant que l'admin n'a pas renseigné l'interface — zéro commande au
        # moment voulu.
        runner(["systemctl", "enable", "--now", "transcria-meeting-runner"])
        runner(["systemctl", "enable", "--now", "transcria-meet-poller"])
        console.ok(f"unités systemd installées et démarrées (DORMANTES) : {unit_path}, "
                   f"{meet_unit_path}")


def _ensure_meeting_ref_key(plan: ConnectorsPlan, console) -> None:
    """Pose TRANSCRIA_MEETING_REF_KEY dans .env si absente — une étape admin de moins.

    La clé est GÉNÉRÉE ici (Fernet) et jamais affichée ; sans elle la fonctionnalité refuse
    de démarrer (contrat meeting_ref_crypto : pas de repli en clair). Idempotent : une clé
    existante n'est JAMAIS remplacée (la remplacer rendrait les sessions indéchiffrables)."""
    env_path = plan.env_file or (plan.repo_root / ".env")
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if "TRANSCRIA_MEETING_REF_KEY" in existing:
        console.ok("clé de chiffrement des références déjà présente (.env)")
        return
    from cryptography.fernet import Fernet

    with env_path.open("a", encoding="utf-8") as fh:
        fh.write("\n# Chiffrement des références de réunion (généré par la phase connectors)\n")
        fh.write(f"TRANSCRIA_MEETING_REF_KEY={Fernet.generate_key().decode()}\n")
    console.ok(f"clé de chiffrement générée et posée dans {env_path}")


def _unit_text(*, repo_root: str, venv_python: str, config_path: str) -> str:
    """Texte de l'unité du meeting-runner — exposé pour que les tests puissent l'inspecter
    sans écrire dans /etc."""
    return SYSTEMD_UNIT_TEMPLATE.format(repo_root=repo_root, venv_python=venv_python,
                                        config_path=config_path)

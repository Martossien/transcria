"""Mise à niveau outillée — chantier C1.2 (docs/archive/RELEASE_0.2.0.md).

Transforme la tradition orale (« git pull && alembic upgrade && restart ») en une
opération SÛRE et reproductible :

1. **sauvegarde AUTOMATIQUE avant** (C1.1) — le rollback, c'est la restauration ;
2. bascule du code (checkout d'un tag / pull) ;
3. migration Alembic ;
4. redémarrage séquencé des services ;
5. contrôle de santé (``/ready``) + rappel de la vérification (doctor / walkthrough).

``--check`` (dry-run) énumère les étapes SANS rien exécuter. La logique de PLAN est
pure et testée ; l'exécution réelle passe par un runner injectable.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UpgradeStep:
    """Une étape de mise à niveau : description humaine + commande (ou action interne)."""

    label: str
    command: list[str] | None = None
    internal: str | None = None   # "backup" | "healthcheck" — exécutées en Python


def build_plan(
    *,
    target_ref: str | None,
    do_pull: bool,
    restart_units: list[str],
    ready_url: str,
    repo_dir: str | None = None,
) -> list[UpgradeStep]:
    """Construit la séquence d'une mise à niveau (pur — testable sans effet de bord).

    ``repo_dir`` : répertoire du dépôt — quand il est fourni, chaque commande git
    porte ``-c safe.directory=<repo_dir>`` (portée commande, aucun état global).
    Sans ce grant, git EN ROOT refuse un dépôt possédé par un autre utilisateur
    (« dubious ownership ») — vécu au premier test réel du oneshot de mise à
    niveau (2026-08-05, exit 128) : le clone appartient à l'opérateur, le
    oneshot tourne en root, et TOUTE mise à niveau échouait.
    """
    git = ["git"] + (["-c", f"safe.directory={repo_dir}"] if repo_dir else [])
    steps: list[UpgradeStep] = [
        UpgradeStep("Sauvegarde de sécurité (rollback = restauration)", internal="backup"),
    ]
    if target_ref:
        # Un tag fraîchement publié n'existe pas encore dans le clone local : la
        # vérification de version lit l'API GitHub (métadonnées seulement), rien
        # n'avait jamais rapatrié le tag — le checkout échouait sur « pathspec
        # inconnu » pour toute VRAIE montée (masqué tant qu'on testait un tag
        # déjà local, découvert au même test réel).
        steps.append(UpgradeStep("Récupération des versions publiées",
                                 command=git + ["fetch", "--tags", "origin"]))
        steps.append(UpgradeStep(f"Bascule du code sur {target_ref}",
                                 command=git + ["checkout", target_ref]))
    elif do_pull:
        steps.append(UpgradeStep("Récupération des dernières modifications",
                                 command=git + ["pull", "--ff-only"]))
    import sys as _sys

    steps.append(UpgradeStep("Migration de la base (Alembic)",
                             command=[_sys.executable, "-m", "alembic", "upgrade", "head"]))
    for unit in restart_units:
        steps.append(UpgradeStep(f"Redémarrage du service {unit}",
                                 command=["sudo", "systemctl", "restart", unit]))
    steps.append(UpgradeStep(f"Contrôle de santé ({ready_url})", internal="healthcheck"))
    return steps


class UpgradeError(Exception):
    """Échec d'une étape de mise à niveau (message actionnable)."""


def run_plan(
    steps: list[UpgradeStep],
    *,
    backup_fn,
    healthcheck_fn,
    runner=subprocess.run,
    echo=print,
    on_step=None,
) -> dict:
    """Exécute la séquence. Toute étape en échec ARRÊTE la mise à niveau (le backup
    initial permet un rollback manuel par restauration). ``on_step(i, total, label)``
    (optionnel) permet à un orchestrateur de journaliser la progression (fichier
    d'état de la page Maintenance) sans analyser la sortie de ``echo``."""
    done: list[str] = []
    for i, step in enumerate(steps, 1):
        if on_step is not None:
            on_step(i, len(steps), step.label)
        echo(f"[{i}/{len(steps)}] {step.label}…")
        if step.internal == "backup":
            archive = backup_fn()
            echo(f"    → sauvegarde : {archive}")
        elif step.internal == "healthcheck":
            if not healthcheck_fn():
                raise UpgradeError(
                    "le service ne répond pas à /ready après le redémarrage — "
                    "consultez les journaux (journalctl -u transcria) ; en dernier "
                    "recours, restaurez la sauvegarde initiale.")
            echo("    → service opérationnel")
        elif step.command:
            proc = runner(step.command, capture_output=True, text=True)
            if proc.returncode != 0:
                raise UpgradeError(
                    f"étape « {step.label} » en échec (code {proc.returncode}) : "
                    f"{proc.stderr.strip()[:400]}\nLes étapes déjà faites : "
                    f"{', '.join(done) or 'aucune'}. Rollback = restauration de la sauvegarde.")
        done.append(step.label)
    return {"steps": done}


def default_ready_check(url: str, *, timeout: float = 5.0, attempts: int = 30) -> bool:
    """Interroge ``/ready`` jusqu'à réponse 200 (le service redémarre en quelques s)."""
    import time

    import requests

    for _ in range(attempts):
        try:
            if requests.get(url, timeout=timeout).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def changelog_excerpt(changelog_path: Path, max_lines: int = 30) -> str:
    """Extrait la section la plus récente du CHANGELOG (« quoi de neuf »)."""
    if not changelog_path.exists():
        return ""
    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    started = False
    for line in lines:
        if line.startswith("## "):
            if started:
                break
            started = True
        if started:
            out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)

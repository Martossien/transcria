"""Mise à disposition du **serveur** PostgreSQL : sonde, offre, vérification.

Constat (issue #14, 2e testeur externe) : sans `psql`, l'installation bascule en SQLite
et l'annonce — mais l'utilisateur l'apprend au moment où il ne peut plus rien y faire.
Le précédent `ffmpeg` (0.4.4) a montré la bonne forme : **proposer**, jamais installer en
douce, puis **rejouer la sonde qui fait foi** plutôt que de croire le code retour du
gestionnaire de paquets.

Deux situations distinctes, deux réponses distinctes :

* `install` — aucun `psql` : proposer d'installer le serveur (apt/dnf) ;
* `start`  — `psql` présent mais aucun serveur ne répond : proposer de le démarrer, ce
  qui est bien moins invasif que d'installer quoi que ce soit.

Le **consentement** reste dans `install.sh` (comme pour ffmpeg) : ce module décide de
l'action à proposer, sait quelles commandes la réalisent, les exécute et **re-sonde**.

Contrainte : appelé PRÉ-VENV par le python système → stdlib uniquement.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from transcria.installer.messages import t
from transcria.installer.packages import (
    ConsoleLike,
    Runner,
    Step,
    Which,
    detect_package_manager,
    emit,
    install_steps,
    privilege_prefix,
    run_command,
    run_steps,
    running_as_root,
)

# Aucune action possible / nécessaire.
ACTION_NONE = "none"
ACTION_INSTALL = "install"
ACTION_START = "start"
# Cas à décrire, pas à corriger : pas de gestionnaire de paquets connu, ou aucun droit.
ACTION_IMPOSSIBLE = "impossible"


@dataclass(frozen=True)
class ServerState:
    """Ce que la machine offre AUJOURD'HUI, avant toute action."""

    psql: bool
    reachable: bool
    package_manager: str = ""  # "apt" | "dnf" | ""
    can_admin: bool = False    # root ou sudo
    have_systemctl: bool = False
    have_service: bool = False
    sudo_prefix: tuple[str, ...] = field(default_factory=tuple)


def probe_server(
    *,
    which: Which = shutil.which,
    run: Runner = run_command,
    is_root: bool | None = None,
    os_release: Path = Path("/etc/os-release"),
) -> ServerState:
    """Sonde l'état réel : client, serveur qui répond, moyens d'agir.

    `pg_isready` absent ⇒ serveur réputé injoignable **seulement si** `psql` manque
    aussi ; un client sans `pg_isready` (install exotique) reste réputé joignable, comme
    ailleurs dans l'installateur — on ne refuse jamais PostgreSQL sur une sonde absente.
    """
    root = running_as_root(is_root)
    prefix = privilege_prefix(which=which, is_root=root)
    psql = which("psql") is not None
    isready = which("pg_isready")
    if isready is None:
        reachable = psql
    else:
        reachable = run([isready, "-q"]) == 0

    return ServerState(
        psql=psql,
        reachable=reachable,
        package_manager=detect_package_manager(which=which, os_release=os_release),
        can_admin=root or bool(prefix),
        have_systemctl=which("systemctl") is not None,
        have_service=which("service") is not None,
        sudo_prefix=prefix,
    )


def decide_action(state: ServerState) -> str:
    """Action à PROPOSER (le consentement est demandé par install.sh)."""
    if state.psql and state.reachable:
        return ACTION_NONE
    if not state.can_admin:
        return ACTION_IMPOSSIBLE
    if not state.psql:
        return ACTION_INSTALL if state.package_manager else ACTION_IMPOSSIBLE
    # Client présent, serveur muet : le démarrer suffit (initdb inclus côté RHEL).
    if state.have_systemctl or state.have_service:
        return ACTION_START
    return ACTION_IMPOSSIBLE


def _service_steps(state: ServerState) -> list[Step]:
    prefix = state.sudo_prefix
    steps: list[Step] = []
    if state.package_manager == "dnf":
        # Cluster non initialisé = service qui refuse de démarrer (piège Fedora/RHEL).
        # Toléré : sur un cluster déjà initialisé, la commande sort en erreur.
        steps.append(Step((*prefix, "postgresql-setup", "--initdb"), tolerate_failure=True))
    service_cmd = (*prefix, "service", "postgresql", "start")
    if state.have_systemctl:
        # `systemctl` PRÉSENT ne veut pas dire systemd en PID 1 (conteneur, WSL sans
        # systemd, chroot) : vécu au rejeu réel, la commande sort en « Failed to connect
        # to bus » et le serveur reste muet. D'où le repli SysV quand il existe.
        steps.append(Step(
            (*prefix, "systemctl", "enable", "--now", "postgresql"),
            fallback=service_cmd if state.have_service else (),
        ))
    elif state.have_service:
        steps.append(Step(service_cmd))
    # Ni l'un ni l'autre : on n'invente pas une commande absente — la re-sonde dira
    # honnêtement que le serveur ne répond pas.
    return steps


def plan_steps(state: ServerState, action: str) -> list[Step]:
    """Séquence exacte pour l'action demandée (mêmes commandes que la doc affichée)."""
    if action == ACTION_START:
        return _service_steps(state)
    if action != ACTION_INSTALL:
        return []
    # Le « comment installer un paquet ici » vit dans packages.py (socle partagé avec
    # l'offre ffmpeg) ; ne reste ici que ce qui est PROPRE au serveur : initdb + service.
    # Debian démarre déjà le cluster à l'installation, le service reste un filet.
    steps = install_steps("postgresql", manager=state.package_manager, prefix=state.sudo_prefix)
    return [*steps, *_service_steps(state)] if steps else []


def render_probe_shell(state: ServerState, action: str) -> str:
    """Lignes machine pour install.sh (``eval_named_shell_assignments``)."""
    return (
        f"PG_SERVER_ACTION={action}\n"
        f"PG_SERVER_PSQL={'true' if state.psql else 'false'}\n"
        f"PG_SERVER_REACHABLE={'true' if state.reachable else 'false'}\n"
        f"PG_SERVER_MANAGER={state.package_manager}"
    )


def render_setup_log(*, event: str, value: str = "") -> str:
    """Messages FR/EN de l'offre serveur (mêmes conventions de préfixe que les phases)."""
    if event == "install-start":
        return f"INFO:{t('pgs_install_start')}\n"
    if event == "start-start":
        return f"INFO:{t('pgs_start_start')}\n"
    if event == "step-failed":
        return f"WARN:{t('pgs_step_failed', cmd=value)}\n"
    if event == "ready":
        return f"OK:{t('pgs_ready')}\n"
    if event == "still-unavailable":
        return f"WARN:{t('pgs_still_unavailable')}\n"
    raise ValueError(f"événement serveur PostgreSQL inconnu : {event}")


def ensure_server(
    *,
    console: ConsoleLike,
    state: ServerState | None = None,
    run: Runner = run_command,
    which: Which = shutil.which,
    is_root: bool | None = None,
    os_release: Path = Path("/etc/os-release"),
) -> bool:
    """Réalise l'action puis **re-sonde** : le verdict vient de la sonde, pas d'apt.

    Retourne True si PostgreSQL est utilisable À L'ARRIVÉE (client + serveur qui répond).
    """
    state = state or probe_server(which=which, run=run, is_root=is_root, os_release=os_release)
    action = decide_action(state)
    if action == ACTION_NONE:
        return True
    if action == ACTION_IMPOSSIBLE:
        return False

    emit(console, render_setup_log(event=f"{action}-start"))
    for step in run_steps(plan_steps(state, action), run=run):
        emit(console, render_setup_log(event="step-failed", value=" ".join(step.argv)))

    # Re-sonde COMPLÈTE : c'est elle qui fait foi (leçon de l'offre ffmpeg).
    after = probe_server(which=which, run=run, is_root=is_root, os_release=os_release)
    usable = after.psql and after.reachable
    emit(console, render_setup_log(event="ready" if usable else "still-unavailable"))
    return usable

"""Socle des OFFRES d'installation système : famille de distro, privilèges, exécution.

Partagé par les offres consenties de `install.sh` — `ffmpeg` (issue #9) et le **serveur
PostgreSQL** (issue #14). Chacune décide *ce qu'il lui faut* ; ce module sait *comment*
l'obtenir sur cette machine, et surtout comment **ne pas se tromper de machine** :

* la famille vient de `/etc/os-release`, jamais de la présence d'un binaire (cette
  Fedora de dev porte `/usr/bin/apt-get`, hérité d'un paquet installé de côté) ;
* un binaire absent rend 127, il ne lève pas — une offre ne doit jamais finir en
  traceback ;
* `apt-get` tourne avec `stdin` fermé et `DEBIAN_FRONTEND=noninteractive` : dpkg lit
  stdin et AVALERAIT la réponse de la question suivante de l'installateur (vécu 0.4.4).

Contrainte : appelé PRÉ-VENV par le python système → stdlib uniquement.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from transcria.installer.messages import t

Runner = Callable[[Sequence[str]], int]
Which = Callable[[str], str | None]

# Paquets par famille. `ffmpeg` : Fedora ne distribue pas `ffmpeg` dans ses dépôts de
# base (RPM Fusion) mais `ffmpeg-free`, vérifié suffisant pour nos entrées — décodeurs
# aac / mp3 / opus / flac / pcm présents (ffmpeg-free 7.1.4, Fedora 42). On ne devine
# donc AUCUN dépôt tiers.
PACKAGES: dict[str, dict[str, tuple[str, ...]]] = {
    "ffmpeg": {"apt": ("ffmpeg",), "dnf": ("ffmpeg-free",)},
    "postgresql": {"apt": ("postgresql",), "dnf": ("postgresql-server", "postgresql")},
}


def run_command(cmd: Sequence[str]) -> int:
    """Exécute sans jamais lever : un binaire absent est un échec d'étape, pas un crash."""
    try:
        return subprocess.run(list(cmd), check=False, stdin=subprocess.DEVNULL).returncode
    except OSError:
        return 127


def detect_package_manager(
    *,
    which: Which = shutil.which,
    os_release: Path = Path("/etc/os-release"),
) -> str:
    """Famille de distribution (`apt` / `dnf` / `""`), lue dans `/etc/os-release`.

    La présence du binaire n'est qu'un dernier recours : une Fedora peut porter
    `apt-get`, et y lancer `apt-get install` serait une faute.
    """
    fields: dict[str, str] = {}
    try:
        for raw in os_release.read_text(encoding="utf-8").splitlines():
            key, _, value = raw.partition("=")
            if key:
                fields[key.strip()] = value.strip().strip('"')
    except OSError:
        fields = {}

    family = f"{fields.get('ID', '')} {fields.get('ID_LIKE', '')}".lower().split()
    if any(name in family for name in ("debian", "ubuntu")):
        return "apt" if which("apt-get") else ""
    if any(name in family for name in ("fedora", "rhel", "centos", "rocky", "almalinux", "ol")):
        return "dnf" if which("dnf") else ""
    if which("dnf"):
        return "dnf"
    return "apt" if which("apt-get") else ""


def running_as_root(is_root: bool | None = None) -> bool:
    """`is_root` explicite (tests, appelants qui savent) sinon l'euid réel."""
    return os.geteuid() == 0 if is_root is None else is_root


def privilege_prefix(*, which: Which = shutil.which, is_root: bool | None = None) -> tuple[str, ...]:
    """Préfixe de commande privilégiée : rien en root, `sudo` sinon, vide si indisponible."""
    if running_as_root(is_root):
        return ()
    return ("sudo",) if which("sudo") else ()


@dataclass(frozen=True)
class Step:
    """Une commande d'une séquence d'offre.

    `tolerate_failure` = l'étape peut légitimement échouer (déjà faite) ; `recover` =
    commande à jouer avant UN seul nouvel essai (cache apt jamais rafraîchi) ;
    `fallback` = autre commande visant le MÊME but si la première échoue.
    """

    argv: tuple[str, ...]
    tolerate_failure: bool = False
    recover: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()


def install_steps(package: str, *, manager: str, prefix: Sequence[str] = ()) -> list[Step]:
    """Séquence d'installation du paquet logique `package` pour cette famille."""
    names = PACKAGES.get(package, {}).get(manager, ())
    if not names:
        return []
    if manager == "apt":
        apt = (*prefix, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get")
        return [Step((*apt, "install", "-y", *names), recover=(*apt, "update"))]
    return [Step((*prefix, manager, "install", "-y", *names))]


def describe_command(package: str, *, manager: str) -> str:
    """Commande telle qu'on la MONTRE à l'utilisateur avant de la lancer."""
    names = PACKAGES.get(package, {}).get(manager, ())
    if not names:
        return ""
    installer = "apt-get" if manager == "apt" else manager
    return f"{installer} install {' '.join(names)}"


def run_steps(steps: Sequence[Step], *, run: Runner = run_command) -> list[Step]:
    """Joue la séquence ; retourne les étapes AYANT ÉCHOUÉ (hors échecs tolérés)."""
    failed: list[Step] = []
    for step in steps:
        code = run(step.argv)
        if code != 0 and step.recover:
            run(step.recover)
            code = run(step.argv)
        if code != 0 and step.fallback:
            code = run(step.fallback)
        if code != 0 and not step.tolerate_failure:
            failed.append(step)
    return failed


def render_setup_log(*, event: str, value: str = "") -> str:
    if event == "install-start":
        return f"INFO:{t('pkg_install_start', cmd=value)}\n"
    if event == "step-failed":
        return f"WARN:{t('pkg_step_failed', cmd=value)}\n"
    if event == "installed":
        return f"OK:{t('pkg_installed', name=value)}\n"
    if event == "missing":
        return f"WARN:{t('pkg_missing', name=value)}\n"
    raise ValueError(f"événement d'installation de paquet inconnu : {event}")


def can_offer(package: str, *, manager: str, prefix: Sequence[str], is_root: bool) -> bool:
    """Une offre n'a de sens qu'avec une famille connue, un paquet connu et des droits."""
    return bool(manager) and bool(PACKAGES.get(package, {}).get(manager)) and (is_root or bool(prefix))


class ConsoleLike(Protocol):  # pragma: no cover - contrat minimal, implémenté par Console
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def emit(console: ConsoleLike, rendered: str) -> None:
    """Route chaque ligne rendue vers la console selon son préfixe (OK/INFO/WARN/ERROR)."""
    methods = {"OK": console.ok, "INFO": console.info, "WARN": console.warn, "ERROR": console.error}
    for line in rendered.splitlines():
        prefix, _, text = line.partition(":")
        methods.get(prefix, console.info)(text if prefix in methods else line)


def ensure_package(
    package: str,
    *,
    binary: str,
    console: ConsoleLike,
    which: Which = shutil.which,
    run: Runner = run_command,
    is_root: bool | None = None,
    os_release: Path = Path("/etc/os-release"),
) -> bool:
    """Installe `package` puis VÉRIFIE la présence de `binary` — la sonde fait foi.

    Le code retour du gestionnaire de paquets ne prouve rien (dépôt à jour mais paquet
    absent, installation partielle) : seul le binaire retrouvé compte.
    """
    manager = detect_package_manager(which=which, os_release=os_release)
    prefix = privilege_prefix(which=which, is_root=is_root)
    steps = install_steps(package, manager=manager, prefix=prefix)
    if not steps:
        return which(binary) is not None

    emit(console, render_setup_log(event="install-start", value=describe_command(package, manager=manager)))
    for step in run_steps(steps, run=run):
        emit(console, render_setup_log(event="step-failed", value=" ".join(step.argv)))

    found = which(binary) is not None
    emit(console, render_setup_log(event="installed" if found else "missing", value=binary))
    return found

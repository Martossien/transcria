"""Amorçage des prérequis OS pour tester `install.sh` dans des conteneurs vierges.

`install.sh` ne pose PAS les prérequis système : il exige Python 3.11+, `ffmpeg`, le
module venv, git, un compilateur et PostgreSQL **déjà présents** (cf. la barrière
prérequis qui ne fait qu'émettre « Installer avec: apt install … »). Une distribution
vierge n'a rien de tout ça : ce module décrit, par distribution, la séquence de
commandes qui amène le conteneur au point où `install.sh` peut s'exécuter.

C'est exactement la **surface de portabilité** qu'on veut éprouver : apt vs dnf, et les
pièges réels (ffmpeg absent des dépôts de base RHEL/Fedora → EPEL/RPM Fusion). La
logique est PURE (génération de commandes) → testable en CI sans Docker ; l'exécution
réelle est faite par l'orchestrateur `scripts/verify_install_matrix.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DistroSpec:
    """Recette d'amorçage d'une distribution vierge."""

    distro_id: str
    base_image: str
    package_manager: str  # "apt" | "dnf"
    packages: tuple[str, ...]
    pre_commands: tuple[str, ...] = field(default=())  # dépôts, refresh d'index…
    install_template: str = ""  # gabarit recevant {pkgs}


# Prérequis communs (noms normalisés par distro ci-dessous) : Python 3.11+, venv/pip,
# ffmpeg, git, compilateur C/C++, make, curl, certificats, PostgreSQL serveur + client,
# sudo (install.sh privilégie certaines étapes). PostgreSQL est requis : SQLite est
# refusé pour les rôles à base applicative (web/scheduler/all/migrate).
_DEBIAN_LIKE_PACKAGES = (
    "python3", "python3-venv", "python3-pip", "python3-dev",
    "ffmpeg", "git", "build-essential", "curl", "ca-certificates",
    "postgresql", "postgresql-client", "sudo",
)

# Sur RHEL/Fedora, ffmpeg n'est PAS dans les dépôts de base : Fedora l'a via RPM Fusion,
# Rocky via EPEL + RPM Fusion. On encode ce piège dans les pre_commands.
_RHEL_LIKE_PACKAGES = (
    "python3.11", "python3.11-pip", "python3.11-devel",
    "ffmpeg", "git", "gcc", "gcc-c++", "make", "curl", "ca-certificates",
    "postgresql-server", "postgresql", "sudo",
)
_FEDORA_PACKAGES = (
    "python3", "python3-pip", "python3-devel",
    "ffmpeg", "git", "gcc", "gcc-c++", "make", "curl", "ca-certificates",
    "postgresql-server", "postgresql", "sudo",
)

DISTROS: dict[str, DistroSpec] = {
    "ubuntu2204": DistroSpec(
        distro_id="ubuntu2204", base_image="ubuntu:22.04", package_manager="apt",
        packages=_DEBIAN_LIKE_PACKAGES,
        pre_commands=("export DEBIAN_FRONTEND=noninteractive", "apt-get update -y"),
        install_template="apt-get install -y --no-install-recommends {pkgs}",
    ),
    "ubuntu2404": DistroSpec(
        distro_id="ubuntu2404", base_image="ubuntu:24.04", package_manager="apt",
        packages=_DEBIAN_LIKE_PACKAGES,
        pre_commands=("export DEBIAN_FRONTEND=noninteractive", "apt-get update -y"),
        install_template="apt-get install -y --no-install-recommends {pkgs}",
    ),
    "debian12": DistroSpec(
        distro_id="debian12", base_image="debian:12", package_manager="apt",
        packages=_DEBIAN_LIKE_PACKAGES,
        pre_commands=("export DEBIAN_FRONTEND=noninteractive", "apt-get update -y"),
        install_template="apt-get install -y --no-install-recommends {pkgs}",
    ),
    "fedora41": DistroSpec(
        distro_id="fedora41", base_image="fedora:41", package_manager="dnf",
        packages=_FEDORA_PACKAGES,
        pre_commands=(
            # ffmpeg (non-free) vit dans RPM Fusion sur Fedora.
            "dnf install -y "
            "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm",
            "dnf -y makecache",
        ),
        install_template="dnf install -y {pkgs}",
    ),
    "rocky9": DistroSpec(
        distro_id="rocky9", base_image="rockylinux:9", package_manager="dnf",
        packages=_RHEL_LIKE_PACKAGES,
        pre_commands=(
            # Rocky/RHEL 9 : Python 3.11 dispo en paquet ; ffmpeg exige EPEL + RPM Fusion.
            "dnf install -y dnf-plugins-core epel-release",
            "dnf config-manager --set-enabled crb || dnf config-manager --enable crb || true",
            "dnf install -y "
            "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-9.noarch.rpm",
            "dnf -y makecache",
        ),
        install_template="dnf install -y {pkgs}",
    ),
}


def available_distros() -> list[str]:
    return sorted(DISTROS)


def get_distro(distro_id: str) -> DistroSpec:
    try:
        return DISTROS[distro_id]
    except KeyError:
        raise ValueError(
            f"Distribution inconnue : {distro_id!r}. Disponibles : {', '.join(available_distros())}"
        ) from None


def bootstrap_commands(distro_id: str) -> list[str]:
    """Séquence shell amenant un conteneur vierge au point où `install.sh` peut tourner."""
    spec = get_distro(distro_id)
    cmds = list(spec.pre_commands)
    cmds.append(spec.install_template.format(pkgs=" ".join(spec.packages)))
    return cmds

"""Tests de l'amorçage OS par distribution (transcria.deploy.distro_bootstrap).

Pur, sans Docker : on vérifie que chaque distribution cible mappe le bon gestionnaire
de paquets et embarque les prérequis réels de `install.sh` (Python 3.11+, ffmpeg, venv/
compilateur, PostgreSQL), y compris les pièges de dépôts (RPM Fusion/EPEL pour ffmpeg).
"""
import pytest

from transcria.deploy.distro_bootstrap import (
    DISTROS,
    available_distros,
    bootstrap_commands,
    get_distro,
)

ALL = available_distros()


class TestCatalog:
    def test_expected_distros_present(self):
        assert set(ALL) == {"ubuntu2204", "ubuntu2404", "debian12", "fedora41", "rocky9"}

    def test_unknown_distro_raises_with_help(self):
        with pytest.raises(ValueError, match="Disponibles"):
            get_distro("arch")

    @pytest.mark.parametrize("distro_id", ALL)
    def test_base_image_and_manager_consistent(self, distro_id):
        spec = DISTROS[distro_id]
        assert spec.base_image
        assert spec.package_manager in ("apt", "dnf")


class TestPackageManagerMapping:
    @pytest.mark.parametrize("distro_id", ["ubuntu2204", "ubuntu2404", "debian12"])
    def test_debian_like_use_apt(self, distro_id):
        assert DISTROS[distro_id].package_manager == "apt"

    @pytest.mark.parametrize("distro_id", ["fedora41", "rocky9"])
    def test_rhel_like_use_dnf(self, distro_id):
        assert DISTROS[distro_id].package_manager == "dnf"


class TestPrerequisitesCoverage:
    @pytest.mark.parametrize("distro_id", ALL)
    def test_ffmpeg_present_everywhere(self, distro_id):
        assert "ffmpeg" in DISTROS[distro_id].packages

    @pytest.mark.parametrize("distro_id", ALL)
    @pytest.mark.parametrize("tool", ["numactl", "lsof", "zstd"])
    def test_runtime_tools_present(self, distro_id, tool):
        # numactl (lanceur LLM llama.cpp), lsof (ports LLM), zstd (tarballs Ollama +
        # binaires llama.cpp précompilés) : lacunes réelles rencontrées en distro vierge.
        assert tool in DISTROS[distro_id].packages

    @pytest.mark.parametrize("distro_id", ALL)
    def test_postgres_server_present(self, distro_id):
        pkgs = " ".join(DISTROS[distro_id].packages)
        assert "postgresql" in pkgs

    @pytest.mark.parametrize("distro_id", ALL)
    def test_python_present(self, distro_id):
        pkgs = " ".join(DISTROS[distro_id].packages)
        assert "python3" in pkgs  # python3 ou python3.11

    @pytest.mark.parametrize("distro_id", ALL)
    def test_compiler_present(self, distro_id):
        pkgs = " ".join(DISTROS[distro_id].packages)
        # build-essential (Debian) ou gcc (RHEL/Fedora) tirent le compilateur C.
        assert "build-essential" in pkgs or "gcc" in pkgs


class TestRepoTraps:
    def test_rocky_enables_epel_and_rpmfusion_for_ffmpeg(self):
        # Le piège réel : ffmpeg absent des dépôts de base RHEL → EPEL + RPM Fusion requis.
        pre = " ".join(DISTROS["rocky9"].pre_commands)
        assert "epel-release" in pre
        assert "rpmfusion" in pre

    def test_fedora_enables_rpmfusion_for_ffmpeg(self):
        pre = " ".join(DISTROS["fedora41"].pre_commands)
        assert "rpmfusion" in pre

    def test_debian_like_refresh_index_noninteractive(self):
        pre = " ".join(DISTROS["debian12"].pre_commands)
        assert "apt-get update" in pre
        assert "DEBIAN_FRONTEND=noninteractive" in pre


class TestBootstrapCommands:
    @pytest.mark.parametrize("distro_id", ALL)
    def test_ends_with_install_of_all_packages(self, distro_id):
        cmds = bootstrap_commands(distro_id)
        spec = DISTROS[distro_id]
        # La dernière commande installe TOUS les paquets déclarés.
        install = cmds[-1]
        for pkg in spec.packages:
            assert pkg in install
        assert spec.package_manager == "apt" and "apt-get install" in install \
            or spec.package_manager == "dnf" and "dnf install" in install

    @pytest.mark.parametrize("distro_id", ALL)
    def test_pre_commands_precede_install(self, distro_id):
        cmds = bootstrap_commands(distro_id)
        spec = DISTROS[distro_id]
        assert cmds[: len(spec.pre_commands)] == list(spec.pre_commands)

    def test_unknown_distro_raises(self):
        with pytest.raises(ValueError):
            bootstrap_commands("gentoo")

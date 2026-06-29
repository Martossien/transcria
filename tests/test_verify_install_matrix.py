"""Tests CI (GPU-free) de l'orchestrateur de matrice d'install (scripts/verify_install_matrix.py).

On valide la logique de DÉCISION pure : catalogue des topologies, argv ``docker run``
(accès GPU conditionnel, montage du dépôt, ports publiés), commande d'install et mise en
place de la config. L'exécution réelle (Docker + GPU) est couverte par le test gated
``test_gpu_e2e_install_matrix`` ci-dessous, sauté hors machine GPU.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _load_orchestrator():
    # Le script vit dans scripts/ (pas un package) → import par chemin.
    spec = importlib.util.spec_from_file_location("verify_install_matrix", _REPO / "scripts" / "verify_install_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["verify_install_matrix"] = mod
    spec.loader.exec_module(mod)
    return mod


vim = _load_orchestrator()


class TestTopologyCatalog:
    def test_two_topologies(self):
        topos = vim.topologies()
        assert set(topos) == {"all-in-one", "frontale-split"}

    def test_all_in_one_is_single_gpu_container(self):
        t = vim.topologies()["all-in-one"]
        assert len(t.containers) == 1
        assert t.containers[0].gpu is True
        assert t.containers[0].install_profile == "all-in-one"
        assert t.gpu_container == t.containers[0].name

    def test_frontale_split_separates_cpu_front_and_gpu_node(self):
        t = vim.topologies()["frontale-split"]
        by_profile = {c.install_profile: c for c in t.containers}
        assert set(by_profile) == {"web", "resource-node"}
        assert by_profile["web"].gpu is False          # frontale = CPU
        assert by_profile["resource-node"].gpu is True  # nœud = GPU
        # Le conteneur sondé pour le GPU est bien le nœud, pas la frontale.
        assert t.gpu_container == by_profile["resource-node"].name

    def test_db_roles_only_on_app_tiers_not_gpu_node(self):
        # PostgreSQL pour web/all (base applicative) ; le resource-node = nœud GPU pur, sans base.
        t = vim.topologies()["frontale-split"]
        by_profile = {c.install_profile: c for c in t.containers}
        assert by_profile["web"].needs_db is True
        assert by_profile["resource-node"].needs_db is False
        assert vim.topologies()["all-in-one"].containers[0].needs_db is True

    def test_frontale_uses_remote_config(self):
        t = vim.topologies()["frontale-split"]
        front = next(c for c in t.containers if c.install_profile == "web")
        assert "frontale" in front.config_example


class TestDockerRunArgv:
    def _spec(self, gpu):
        return vim.ContainerSpec(
            name="c1", install_profile="all-in-one", config_example="config.example.yaml",
            gpu=gpu, launch=["x"], published={7870: 7870}, health_internal_port=7870, needs_db=True,
        )

    def test_gpu_container_requests_cdi_device(self):
        argv = vim.docker_run_argv(self._spec(True), "ubuntu:24.04", _REPO)
        assert "--device" in argv
        assert "nvidia.com/gpu=all" in argv

    def test_cpu_container_has_no_gpu_device(self):
        argv = vim.docker_run_argv(self._spec(False), "ubuntu:24.04", _REPO)
        assert "nvidia.com/gpu=all" not in argv

    def test_mounts_repo_readonly_and_publishes_port(self):
        argv = vim.docker_run_argv(self._spec(True), "debian:12", _REPO)
        joined = " ".join(argv)
        assert f"{_REPO}:/src:ro" in joined
        assert "-p" in argv and "7870:7870" in argv
        assert argv[-3:] == ["debian:12", "sleep", "infinity"]
        assert "--network" in argv and vim.NETWORK in argv

    def test_secrets_passed_by_reference_not_value(self):
        # Sécurité : un secret passé par référence (`-e NAME`) ne doit JAMAIS apparaître
        # avec sa valeur dans l'argv (sinon fuite dans les logs / `ps`).
        argv = vim.docker_run_argv(self._spec(True), "ubuntu:24.04", _REPO,
                                   env_set={"TRANSCRIA_DATABASE_URL": "dsn"},
                                   env_passthrough=("HF_TOKEN",))
        # HF_TOKEN passé sans valeur (référence à l'env hôte).
        assert "HF_TOKEN" in argv
        assert not any(a.startswith("HF_TOKEN=") for a in argv)
        # Le DSN de test (valeur jetable) est, lui, explicite.
        assert "TRANSCRIA_DATABASE_URL=dsn" in argv


class TestInstallCommand:
    def test_runs_native_install_with_profile_no_service_noninteractive(self):
        cmd = vim.install_command("resource-node", None, pg_existing=False)
        assert "./install.sh --profile resource-node" in cmd
        assert "--no-service" in cmd
        assert "--non-interactive" in cmd  # conteneur sans TTY → pas de prompt bloquant
        # On veut le VRAI install (torch CUDA + modèles) : surtout PAS --skip-deps.
        assert "--skip-deps" not in cmd

    def test_pg_existing_only_for_db_roles(self):
        assert "--pg-existing" in vim.install_command("web", None, pg_existing=True)
        assert "--pg-existing" not in vim.install_command("resource-node", None, pg_existing=False)

    def test_cuda_flag_appended_when_given(self):
        assert "--cuda cu126" in vim.install_command("all-in-one", "cu126", pg_existing=True)

    def test_no_cuda_flag_when_absent(self):
        assert "--cuda" not in vim.install_command("all-in-one", None, pg_existing=True)


class TestConfigSetupCommand:
    def test_copies_example_and_substitutes_node_ip(self):
        cmd = vim.config_setup_command("config.frontale.example.yaml", "node-x")
        assert "cp /app/config.frontale.example.yaml /app/config.yaml" in cmd
        assert "s/NODE_IP/node-x/g" in cmd

    def test_injects_dsn_when_given(self):
        dsn = "postgresql+psycopg://transcria:pw@host:5432/transcria"
        cmd = vim.config_setup_command("config.example.yaml", "n", dsn=dsn)
        assert f'database_url: "{dsn}"' in cmd
        assert "sed -i 's|database_url" in cmd  # délimiteur | car le DSN contient des /

    def test_no_dsn_substitution_when_absent(self):
        cmd = vim.config_setup_command("config.resource-node.example.yaml", "n")
        assert "database_url" not in cmd

    def test_node_name_is_the_network_container_name(self):
        # La frontale joint le nœud par son nom DNS sur le réseau Docker.
        t = vim.topologies()["frontale-split"]
        node = next(c for c in t.containers if c.gpu)
        assert node.name == vim.NODE_NAME


@pytest.mark.gpu_e2e
def test_gpu_e2e_install_matrix():
    """E2E RÉEL (Docker + GPU) — sauté sauf TRANSCRIA_GPU_E2E=1 (machine GPU).

    Joue la topologie all-in-one sur une distro, de l'install.sh aux livrables. Sur un
    runner sans GPU, ce test est collecté puis SKIPPÉ (aucune économie : la logique est
    couverte ci-dessus ; seul le run matériel est différé à la machine GPU)."""
    import os

    if os.environ.get("TRANSCRIA_GPU_E2E") != "1":
        pytest.skip("E2E GPU non demandé (positionner TRANSCRIA_GPU_E2E=1 sur la machine GPU)")
    distro = os.environ.get("TRANSCRIA_GPU_E2E_DISTRO", "ubuntu2404")
    topo = vim.topologies()[os.environ.get("TRANSCRIA_GPU_E2E_TOPOLOGY", "all-in-one")]
    vim.preflight_gpu()
    vim.run_topology(
        topo, distro, _REPO / "tests" / "test2.mp3",
        profile=os.environ.get("TRANSCRIA_GPU_E2E_PROFILE"),
        username=os.environ.get("TRANSCRIA_ADMIN_USER", "admin"),
        password=os.environ.get("TRANSCRIA_ADMIN_PASSWORD", "admin-change-me"),
        cuda=os.environ.get("TRANSCRIA_GPU_E2E_CUDA"),
        keep_up=False,
    )

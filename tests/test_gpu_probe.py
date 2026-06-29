"""Tests de la logique PURE des sondes GPU (transcria.deploy.gpu_probe).

Aucun GPU ni Docker requis : on injecte des sorties figées de `nvidia-smi -L` et de
la sonde torch. Couvre le point dur exigé — un repli CPU silencieux (torch.cuda
indisponible) doit produire un verdict d'ÉCHEC, jamais un succès.
"""
from transcria.deploy.gpu_probe import (
    GpuVerdict,
    capabilities_have_gpu,
    parse_nvidia_smi_l,
    parse_torch_probe,
    probe_container_gpu,
    verdict_from_outputs,
)

SMI_2GPU = (
    "GPU 0: NVIDIA RTX 6000 Ada Generation (UUID: GPU-aaaa-1111)\n"
    "GPU 1: NVIDIA A100-SXM4-80GB (UUID: GPU-bbbb-2222)\n"
)
SMI_1GPU = "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-cccc-3333)\n"


class TestParseNvidiaSmi:
    def test_extracts_multiple_names(self):
        assert parse_nvidia_smi_l(SMI_2GPU) == ["NVIDIA RTX 6000 Ada Generation", "NVIDIA A100-SXM4-80GB"]

    def test_single_gpu(self):
        assert parse_nvidia_smi_l(SMI_1GPU) == ["NVIDIA GeForce RTX 4090"]

    def test_empty_output(self):
        assert parse_nvidia_smi_l("") == []
        assert parse_nvidia_smi_l("No devices were found\n") == []


class TestParseTorchProbe:
    def test_cuda_available(self):
        assert parse_torch_probe("CUDA True 2") == (True, 2)

    def test_cuda_unavailable(self):
        assert parse_torch_probe("CUDA False 0") == (False, 0)

    def test_garbage_output(self):
        assert parse_torch_probe("ModuleNotFoundError: torch") == (None, None)


class TestVerdictFromOutputs:
    def test_gpu_visible_and_usable_is_ok(self):
        v = verdict_from_outputs(SMI_1GPU, "CUDA True 1")
        assert v.ok is True
        assert v.gpu_names == ["NVIDIA GeForce RTX 4090"]
        assert v.torch_cuda is True and v.device_count == 1

    def test_no_gpu_listed_fails(self):
        v = verdict_from_outputs("", "CUDA True 1")
        assert v.ok is False
        assert "aucun GPU" in v.detail

    def test_cuda_unavailable_fails_loudly(self):
        # Le cœur de l'exigence : GPU visible mais torch en CPU → ÉCHEC (pas de repli muet).
        v = verdict_from_outputs(SMI_1GPU, "CUDA False 0")
        assert v.ok is False
        assert "repli CPU" in v.detail

    def test_torch_probe_unreadable_fails(self):
        v = verdict_from_outputs(SMI_1GPU, "boom")
        assert v.ok is False

    def test_zero_devices_fails(self):
        v = verdict_from_outputs(SMI_1GPU, "CUDA True 0")
        assert v.ok is False


class TestCapabilitiesHaveGpu:
    def test_node_with_gpus(self):
        ok, msg = capabilities_have_gpu({"gpus": [{"index": 0, "free_mb": 40000, "total_mb": 81920}]})
        assert ok is True
        assert "81920" in msg

    def test_node_without_gpus(self):
        ok, msg = capabilities_have_gpu({"gpus": []})
        assert ok is False

    def test_node_with_zero_vram_gpu_rejected(self):
        ok, _ = capabilities_have_gpu({"gpus": [{"index": 0, "total_mb": 0}]})
        assert ok is False

    def test_legacy_devices_key(self):
        ok, _ = capabilities_have_gpu({"devices": [{"index": 0, "total": 24000}]})
        assert ok is True


class TestProbeContainerGpu:
    def test_happy_path_with_injected_runner(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            return SMI_2GPU if "nvidia-smi" in argv else "CUDA True 2"

        v = probe_container_gpu("transcria-node", runner)
        assert v.ok is True
        assert v.device_count == 2
        # On a bien sondé via docker exec sur le bon conteneur.
        assert all(a[:3] == ["docker", "exec", "transcria-node"] for a in calls)

    def test_runner_raising_on_smi_is_actionable_failure(self):
        def runner(argv):
            raise RuntimeError("container not running")

        v = probe_container_gpu("dead", runner)
        assert v.ok is False
        assert "nvidia-smi" in v.detail and "dead" in v.detail

    def test_uses_custom_python_bin(self):
        seen = {}

        def runner(argv):
            if "nvidia-smi" in argv:
                return SMI_1GPU
            seen["py"] = argv[3]
            return "CUDA True 1"

        probe_container_gpu("c", runner, python_bin="/usr/bin/python3")
        assert seen["py"] == "/usr/bin/python3"

    def test_returns_gpuverdict_type(self):
        v = probe_container_gpu("c", lambda argv: SMI_1GPU if "nvidia-smi" in argv else "CUDA True 1")
        assert isinstance(v, GpuVerdict)

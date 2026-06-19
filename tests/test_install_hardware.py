from __future__ import annotations

import subprocess

from transcria.install_hardware import (
    detect_nvidia,
    detect_nvidia_vram,
    main,
    parse_nvidia_smi_cuda_version,
    parse_nvidia_smi_gpu_names,
    parse_nvidia_smi_memory_totals,
)


def test_parse_nvidia_smi_gpu_names_ignores_blank_lines():
    assert parse_nvidia_smi_gpu_names("RTX 3090\n\nRTX A6000\n") == ["RTX 3090", "RTX A6000"]


def test_parse_nvidia_smi_memory_totals_ignores_non_numeric_lines():
    assert parse_nvidia_smi_memory_totals("24576\nnot available\n 49152 \n") == [24576, 49152]


def test_parse_nvidia_smi_cuda_version_from_header():
    output = "| NVIDIA-SMI 550.54.14   Driver Version: 550.54.14   CUDA Version: 12.4     |"

    assert parse_nvidia_smi_cuda_version(output) == "12.4"


def test_parse_nvidia_smi_cuda_version_returns_empty_when_missing():
    assert parse_nvidia_smi_cuda_version("NVIDIA-SMI output without cuda") == ""


def test_detect_nvidia_success():
    calls: list[list[str]] = []

    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--query-gpu=name" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="RTX 3090\nRTX 4090\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="CUDA Version: 12.6\n", stderr="")

    gpu_count, cuda_version, warning = detect_nvidia(run)

    assert gpu_count == 2
    assert cuda_version == "12.6"
    assert warning is None
    assert calls == [
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        ["nvidia-smi"],
    ]


def test_detect_nvidia_handles_missing_binary():
    def run(_cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("nvidia-smi")

    gpu_count, cuda_version, warning = detect_nvidia(run)

    assert gpu_count == 0
    assert cuda_version == ""
    assert "nvidia-smi" in str(warning)


def test_detect_nvidia_handles_query_failure():
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="driver unavailable")

    assert detect_nvidia(run) == (0, "", "driver unavailable")


def test_detect_nvidia_vram_success():
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        assert cmd == ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
        return subprocess.CompletedProcess(cmd, 0, stdout="24576\n49152\n", stderr="")

    assert detect_nvidia_vram(run) == (73728, 49152, "24576,49152")


def test_detect_nvidia_vram_handles_missing_binary():
    def run(_cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("nvidia-smi")

    assert detect_nvidia_vram(run) == (0, 0, "")


def test_install_hardware_cli_shell_output(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_hardware.detect_nvidia", lambda: (3, "13.0", None))
    monkeypatch.setattr("transcria.install_hardware.detect_nvidia_vram", lambda: (73728, 49152, "24576,49152"))

    assert main(["--format", "shell"]) == 0

    assert (
        capsys.readouterr().out
        == "GPU_COUNT=3\n"
        "CUDA_VER_FROM_SMI=13.0\n"
        "NVIDIA_WARNING=''\n"
        "GPU_VRAM_TOTAL_MB=73728\n"
        "GPU_VRAM_MAX_MB=49152\n"
        "GPU_SIZES_CSV=24576,49152\n"
    )

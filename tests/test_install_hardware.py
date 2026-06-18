from __future__ import annotations

import subprocess

from transcria.install_hardware import detect_nvidia, main, parse_nvidia_smi_cuda_version, parse_nvidia_smi_gpu_names


def test_parse_nvidia_smi_gpu_names_ignores_blank_lines():
    assert parse_nvidia_smi_gpu_names("RTX 3090\n\nRTX A6000\n") == ["RTX 3090", "RTX A6000"]


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


def test_install_hardware_cli_shell_output(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_hardware.detect_nvidia", lambda: (3, "13.0", None))

    assert main(["--format", "shell"]) == 0

    assert capsys.readouterr().out == "GPU_COUNT=3\nCUDA_VER_FROM_SMI=13.0\nNVIDIA_WARNING=''\n"

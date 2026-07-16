from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from collections.abc import Callable

RunFn = Callable[[list[str]], subprocess.CompletedProcess[str]]

_CUDA_VERSION_RE = re.compile(r"CUDA Version:\s*(?P<version>\d+(?:\.\d+)?)")


def parse_nvidia_smi_gpu_names(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def parse_nvidia_smi_memory_totals(output: str) -> list[int]:
    totals: list[int] = []
    for line in output.splitlines():
        value = line.strip()
        if value.isdigit():
            totals.append(int(value))
    return totals


def parse_nvidia_smi_cuda_version(output: str) -> str:
    match = _CUDA_VERSION_RE.search(output)
    return match.group("version") if match else ""


def _run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)


def detect_nvidia(run: RunFn = _run_command) -> tuple[int, str, str | None]:
    """Retourne `(gpu_count, cuda_version, warning)` pour l'installation."""
    try:
        names_result = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return 0, "", f"nvidia-smi non trouvé ou inutilisable: {exc}"

    if names_result.returncode != 0:
        return 0, "", (names_result.stderr or names_result.stdout or "nvidia-smi a échoué").strip()

    gpu_count = len(parse_nvidia_smi_gpu_names(names_result.stdout))
    cuda_version = ""
    try:
        version_result = run(["nvidia-smi"])
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        version_result = subprocess.CompletedProcess(["nvidia-smi"], returncode=1, stdout="", stderr="")
    if version_result.returncode == 0:
        cuda_version = parse_nvidia_smi_cuda_version(version_result.stdout)
    return gpu_count, cuda_version, None


def detect_nvidia_vram(run: RunFn = _run_command) -> tuple[int, int, str]:
    """Retourne `(total_mb, max_mb, sizes_csv)` pour les GPUs visibles par nvidia-smi."""
    try:
        result = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return 0, 0, ""

    if result.returncode != 0:
        return 0, 0, ""

    totals = parse_nvidia_smi_memory_totals(result.stdout)
    if not totals:
        return 0, 0, ""
    return sum(totals), max(totals), ",".join(str(total) for total in totals)


def _shell_value(value: str | int) -> str:
    return str(value) if isinstance(value, int) else shlex.quote(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Détection matérielle minimale pour install.sh.")
    parser.add_argument("--format", choices=("shell",), default="shell")
    args = parser.parse_args(argv)

    gpu_count, cuda_version, warning = detect_nvidia()
    vram_total_mb, vram_max_mb, gpu_sizes_csv = detect_nvidia_vram()
    if args.format == "shell":
        print(f"GPU_COUNT={gpu_count}")
        print(f"CUDA_VER_FROM_SMI={_shell_value(cuda_version)}")
        print(f"NVIDIA_WARNING={_shell_value(warning or '')}")
        print(f"GPU_VRAM_TOTAL_MB={vram_total_mb}")
        print(f"GPU_VRAM_MAX_MB={vram_max_mb}")
        print(f"GPU_SIZES_CSV={_shell_value(gpu_sizes_csv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

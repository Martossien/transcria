"""Sondes GPU pour la vérification E2E des topologies Docker.

La logique PURE (parsing de sorties + verdicts) est séparée de l'exécution Docker,
afin d'être testable en CI **sans GPU** : `probe_container_gpu` reçoit un *runner*
injecté (en prod = `subprocess`, en test = une fonction qui rejoue des sorties
figées). Le but est de prouver qu'un conteneur **voit ET peut utiliser** le GPU,
ce qui interdit un repli CPU silencieux : si `torch.cuda.is_available()` est faux
dans le conteneur, le pipeline tournerait sur CPU — l'E2E doit échouer, pas passer.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Sonde torch exécutée DANS le conteneur (sortie parsée par `parse_torch_probe`).
TORCH_PROBE_SNIPPET = "import torch;print('CUDA', torch.cuda.is_available(), torch.cuda.device_count())"

# Chemin du Python applicatif dans les images (cf. ENTRYPOINT des Dockerfile.*).
CONTAINER_PYTHON = "/app/venv/bin/python"

_SMI_LINE = re.compile(r"^\s*GPU\s+\d+:\s*(?P<name>.+?)\s*\(UUID:", re.MULTILINE)
_TORCH_OUT = re.compile(r"CUDA\s+(?P<cuda>True|False)\s+(?P<count>\d+)")


@dataclass
class GpuVerdict:
    """Résultat structuré d'une sonde GPU conteneur."""

    ok: bool
    detail: str
    gpu_names: list[str] = field(default_factory=list)
    torch_cuda: bool | None = None
    device_count: int | None = None


def parse_nvidia_smi_l(text: str) -> list[str]:
    """Noms de GPU extraits d'une sortie ``nvidia-smi -L``.

    ``GPU 0: NVIDIA RTX 6000 Ada Generation (UUID: GPU-…)`` → ``NVIDIA RTX 6000 Ada Generation``.
    """
    return [m.group("name").strip() for m in _SMI_LINE.finditer(text)]


def parse_torch_probe(text: str) -> tuple[bool | None, int | None]:
    """Parse la sortie de `TORCH_PROBE_SNIPPET` → (cuda_disponible, nb_devices).

    Retourne ``(None, None)`` si la sortie est illisible (sonde plantée, import torch KO…).
    """
    m = _TORCH_OUT.search(text)
    if not m:
        return None, None
    return m.group("cuda") == "True", int(m.group("count"))


def verdict_from_outputs(smi_text: str, torch_text: str) -> GpuVerdict:
    """Combine les deux sorties en un verdict : GPU listé ET CUDA utilisable in-process."""
    names = parse_nvidia_smi_l(smi_text)
    cuda, count = parse_torch_probe(torch_text)
    if not names:
        return GpuVerdict(False, "nvidia-smi n'a listé aucun GPU dans le conteneur (accès CDI manquant ?)",
                          gpu_names=names, torch_cuda=cuda, device_count=count)
    if cuda is not True:
        return GpuVerdict(False, "torch.cuda.is_available() est faux dans le conteneur → repli CPU silencieux probable",
                          gpu_names=names, torch_cuda=cuda, device_count=count)
    if (count or 0) < 1:
        return GpuVerdict(False, "torch ne voit aucun device CUDA dans le conteneur",
                          gpu_names=names, torch_cuda=cuda, device_count=count)
    return GpuVerdict(True, f"{len(names)} GPU visible(s) + CUDA utilisable ({count} device(s)) : {', '.join(names)}",
                      gpu_names=names, torch_cuda=cuda, device_count=count)


def capabilities_have_gpu(caps: dict) -> tuple[bool, str]:
    """Vérifie que `/capabilities` d'un resource-node énumère au moins un GPU exploitable."""
    gpus = caps.get("gpus") or caps.get("devices") or []
    usable = [g for g in gpus if (g.get("total_mb") or g.get("total") or 0) > 0]
    if not usable:
        return False, f"/capabilities n'énumère aucun GPU exploitable (gpus={gpus!r})"
    total = sum(int(g.get("total_mb") or g.get("total") or 0) for g in usable)
    return True, f"{len(usable)} GPU énuméré(s) par le nœud, VRAM totale ≈ {total} Mo"


def probe_container_gpu(
    container: str,
    runner: Callable[[list[str]], str],
    python_bin: str = CONTAINER_PYTHON,
) -> GpuVerdict:
    """Sonde un conteneur en cours d'exécution : ``nvidia-smi -L`` + sonde torch.

    `runner(argv) -> stdout` est injecté : en prod il enveloppe ``subprocess`` ; en
    test il rejoue des sorties figées (donc testable sans Docker ni GPU). Toute
    exception du runner est convertie en verdict d'échec actionnable.
    """
    try:
        smi = runner(["docker", "exec", container, "nvidia-smi", "-L"])
    except Exception as exc:  # noqa: BLE001
        return GpuVerdict(False, f"`nvidia-smi -L` a échoué dans {container} : {exc}")
    try:
        torch_out = runner(["docker", "exec", container, python_bin, "-c", TORCH_PROBE_SNIPPET])
    except Exception as exc:  # noqa: BLE001
        return GpuVerdict(False, f"sonde torch a échoué dans {container} : {exc}", gpu_names=parse_nvidia_smi_l(smi))
    return verdict_from_outputs(smi, torch_out)

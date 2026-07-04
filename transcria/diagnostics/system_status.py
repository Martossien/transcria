"""État système LOCAL pour la page « Système » — chantier C2.3 (docs/archive/RELEASE_0.2.0.md).

Remplace la dépendance au projet externe « llmdashboard » (DashboardClient) : les
métriques viennent désormais DE LA MACHINE (psutil pour CPU/RAM, NVML/torch pour les
GPU) — plus de service tiers à faire tourner, plus de mode dégradé par défaut.

Le contrat de sortie reproduit celui que consommait le template ``dashboard_status.html``
(clés ``cpu.load``, ``ram.used/total``, ``gpus[]``, ``available``) — zéro changement UI.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _cpu_ram() -> tuple[dict, dict]:
    try:
        import psutil

        load = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        return (
            {"load": round(load, 1), "cores": psutil.cpu_count() or 0},
            {"used": round(mem.used / (1024 ** 3), 1), "total": round(mem.total / (1024 ** 3), 1),
             "percent": round(mem.percent, 1)},
        )
    except Exception as exc:  # noqa: BLE001 — la page Système ne doit jamais casser
        logger.debug("psutil indisponible : %s", exc)
        return {}, {}


def _gpus() -> list[dict]:
    """GPU locaux via NVML (léger, vue HÔTE) puis torch (repli)."""
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            gpus = []
            for i in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                name = pynvml.nvmlDeviceGetName(handle)
                gpus.append({
                    "id": i,
                    "name": name.decode() if isinstance(name, bytes) else str(name),
                    "memory": {
                        "used": round(mem.used / (1024 ** 3), 1),
                        "free": round(mem.free / (1024 ** 3), 1),
                        "total": round(mem.total / (1024 ** 3), 1),
                    },
                })
            return gpus
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001 — NVML absent (frontale CPU) : repli torch
        pass

    try:
        import torch

        if not torch.cuda.is_available():
            return []
        gpus = []
        for i in range(torch.cuda.device_count()):
            free, total = torch.cuda.mem_get_info(i)
            gpus.append({
                "id": i,
                "name": torch.cuda.get_device_name(i),
                "memory": {
                    "used": round((total - free) / (1024 ** 3), 1),
                    "free": round(free / (1024 ** 3), 1),
                    "total": round(total / (1024 ** 3), 1),
                },
            })
        return gpus
    except Exception as exc:  # noqa: BLE001
        logger.debug("GPU locaux illisibles : %s", exc)
        return []


def get_system_status() -> dict:
    """Même forme que l'ancien ``DashboardClient.get_system_status`` — source LOCALE."""
    cpu, ram = _cpu_ram()
    return {
        "cpu": cpu,
        "ram": ram,
        "gpus": _gpus(),
        "services": {},
        "gpu_processes": {},
        "model": "",
        "available": bool(cpu or ram),
    }

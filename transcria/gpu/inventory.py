"""Sonde GPU unique — l'inventaire matériel lu par tout l'arbre (vague B3).

``VRAMManager`` et ``GPUAllocator`` portaient chacun leur copie de la sonde
(``torch.cuda.mem_get_info`` par carte) avec des sémantiques d'erreur
DIVERGENTES : l'allocateur tolérait toute exception, le manager seulement
``ImportError`` — un ``RuntimeError`` CUDA donnait donc deux visions
différentes de la même carte. Une seule implémentation désormais, avec la
politique robuste : toute panne de sonde vaut « aucun GPU » (journalisée en
debug), jamais un crash — les appelants (admission, préemption, dashboards)
dégradent proprement.

Les deux classes DÉLÈGUENT ici (signatures publiques inchangées) ; la forme
historique ``list[dict]`` reste leur contrat d'appel, ``GpuState`` est la
forme typée de ce module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GpuState:
    """État instantané d'une carte : identité + mémoire en Gio."""

    id: int
    name: str
    free_gib: float
    used_gib: float
    total_gib: float
    # Les index rapportés sont DÉJÀ dans l'espace CUDA_VISIBLE_DEVICES du process
    # (torch remappe) — les consommateurs le savent via ce drapeau (cf. cuda_visible).
    cuda_visible_remapped: bool = True

    def as_dict(self) -> dict:
        """Forme historique consommée par les appelants et les fakes de tests."""
        return {
            "id": self.id,
            "name": self.name,
            "cuda_visible_remapped": self.cuda_visible_remapped,
            "memory": {"used": self.used_gib, "free": self.free_gib, "total": self.total_gib},
        }


def snapshot() -> tuple[GpuState, ...]:
    """L'UNIQUE sonde GPU de l'arbre (DoD B3 : grep = 1 site).

    Vide si CUDA indisponible ou si la sonde échoue — jamais d'exception.
    """
    states: list[GpuState] = []
    try:
        import torch  # différé : dépendance lourde de boot, sondée à la demande

        if torch.cuda.is_available():
            for idx in range(torch.cuda.device_count()):
                try:
                    free, total = torch.cuda.mem_get_info(idx)
                except Exception:  # noqa: BLE001 — carte illisible → ignorée, les saines restent
                    logger.debug("GPU %d illisible — ignoré dans l'inventaire", idx, exc_info=True)
                    continue
                states.append(GpuState(
                    id=idx,
                    name=torch.cuda.get_device_name(idx),
                    free_gib=free / (1024 ** 3),
                    used_gib=(total - free) / (1024 ** 3),
                    total_gib=total / (1024 ** 3),
                ))
    except Exception:  # noqa: BLE001 — panne de sonde = inventaire vide, pas un crash
        logger.debug("Sonde GPU indisponible — inventaire vide", exc_info=True)
    return tuple(states)


def legacy_gpu_info() -> list[dict]:
    """Le snapshot sous la forme historique ``list[dict]`` (contrat des appelants)."""
    return [state.as_dict() for state in snapshot()]

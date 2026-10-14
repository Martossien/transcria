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

import ctypes
import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Lecture mémoire via NVML, pour NE PAS créer de contexte CUDA ────────────────
#
# POURQUOI : `torch.cuda.mem_get_info(idx)` sélectionne la carte, donc OUVRE un
# contexte CUDA dessus — ~500 Mio par RTX 5090, ~224 Mio par RTX 5070 Ti (mesuré le
# 2026-10-12). L'inventaire sondant TOUTES les cartes, un process TranscrIA payait
# ~1,2 Gio de VRAM juste pour lire « combien est libre », sur des cartes qu'il
# n'utilise pas. Sur une machine où la LLM d'arbitrage occupe 92 % de deux cartes,
# ces 500 Mio décidaient de la possibilité même de la lancer.
#
# NVML répond à la même question sans contexte. On n'ajoute AUCUNE dépendance Python
# (`pynvml` n'est pas installé) : `libnvidia-ml.so.1` est livrée avec le pilote et se
# charge en ctypes. L'appariement carte↔carte se fait par **identifiant de bus PCI**,
# jamais par index : torch énumère en CUDA_DEVICE_ORDER (FASTEST_FIRST par défaut,
# donc les grosses cartes d'abord) et NVML en ordre PCI — sur une machine hétérogène
# les deux numérotations DIVERGENT (piège vécu le 2026-10-12 : le doctor lisait
# nvidia-smi et l'allocateur torch, avec des index croisés).
#
# Tout échec (pas de lib, pas de `get_device_properties`, bus introuvable) retombe sur
# `mem_get_info` : le comportement historique reste la garantie de dernier recours.
_NVML_SUCCESS = 0

_nvml_lock = threading.Lock()
_nvml_lib: ctypes.CDLL | None = None
_nvml_tried = False


class _NvmlMemory(ctypes.Structure):
    """`nvmlMemory_t` — total/free/used en octets (forme v1, stable)."""

    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def _nvml() -> ctypes.CDLL | None:
    """Charge et initialise NVML UNE fois par process (jamais de shutdown).

    Un échec est mémorisé : pas de rechargement de la bibliothèque à chaque sonde.
    """
    global _nvml_lib, _nvml_tried
    with _nvml_lock:
        if _nvml_tried:
            return _nvml_lib
        _nvml_tried = True
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != _NVML_SUCCESS:
                logger.debug("NVML présent mais nvmlInit_v2 a échoué — repli mem_get_info")
                return None
            _nvml_lib = lib
        except Exception:  # noqa: BLE001 — pas de pilote NVIDIA, lib absente… → repli
            logger.debug("NVML indisponible — repli sur torch.cuda.mem_get_info", exc_info=True)
        return _nvml_lib


def _bus_id(props) -> str | None:
    """`00000000:04:00.0` depuis les champs PCI de `get_device_properties`."""
    try:
        return f"{props.pci_domain_id:08X}:{props.pci_bus_id:02X}:{props.pci_device_id:02X}.0"
    except Exception:  # noqa: BLE001 — torch ancien ou doublure de test minimale
        return None


# Écart toléré entre le total NVML (matériel) et celui de torch (utilisable pilote).
# Mesuré ~1,5 % sur RTX 5090 (32 607 contre 32 109). Au-delà, les deux descripteurs ne
# parlent PAS de la même chose — cas MIG : `nvmlDeviceGetHandleByPciBusId` rend la carte
# PARENTE (80 Go) là où torch voit une TRANCHE (10 Go). On refuse alors la mesure NVML.
_ECART_TOTAL_TOLERE = 0.10


def _nvml_free_total(bus_id: str) -> tuple[int, int] | None:
    """(libre, total) en octets vus par NVML, ou None si NVML ne sait pas répondre.

    Seul le LIBRE sera retenu par l'appelant : le TOTAL publié reste celui de
    `props.total_memory` (32 109 Mio là où NVML annonce 32 607 pour la même carte —
    NVML donne le total MATÉRIEL, torch le total utilisable par le pilote). C'est ce
    dernier que `stt_vram_planner` multiplie par la fraction
    `--gpu-memory-utilization`, et vLLM raisonne sur la même base (« total capacity of
    31.36 GiB »). Le total NVML ne sert donc qu'à VÉRIFIER l'appariement.
    """
    lib = _nvml()
    if lib is None:
        return None
    try:
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByPciBusId_v2(
            bus_id.encode("ascii"), ctypes.byref(handle)
        ) != _NVML_SUCCESS:
            return None
        mem = _NvmlMemory()
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != _NVML_SUCCESS:
            return None
        return int(mem.free), int(mem.total)
    except Exception:  # noqa: BLE001 — NVML capricieux → repli, jamais de crash
        logger.debug("Lecture NVML impossible pour %s — repli", bus_id, exc_info=True)
        return None


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


def _appariement_coherent(lu: tuple[int, int], total_torch: int, idx: int) -> bool:
    """La carte lue par NVML est-elle bien celle que torch décrit ?

    Garde-fou de PORTABILITÉ : sur une machine MIG, le bus PCI mène à la carte parente
    (mémoire du GPU entier) alors que torch voit une tranche. Même protection contre un
    bus mal reconstruit. En cas de doute on rend False et l'appelant reprend
    `mem_get_info` — plus lent (contexte CUDA) mais toujours juste.
    """
    libre, total_nvml = lu
    if total_nvml <= 0 or libre < 0 or libre > total_nvml:
        return False
    if abs(total_nvml - total_torch) / total_nvml > _ECART_TOTAL_TOLERE:
        logger.debug(
            "GPU %d : total NVML %d octets vs torch %d — descripteurs incompatibles "
            "(MIG ?), repli sur mem_get_info", idx, total_nvml, total_torch,
        )
        return False
    return True


def snapshot() -> tuple[GpuState, ...]:
    """L'UNIQUE sonde GPU de l'arbre (DoD B3 : grep = 1 site).

    Vide si CUDA indisponible ou si la sonde échoue — jamais d'exception.
    """
    states: list[GpuState] = []
    try:
        import torch  # différé : dépendance lourde de boot, sondée à la demande

        if torch.cuda.is_available():
            for idx in range(torch.cuda.device_count()):
                # Voie SANS contexte CUDA : identité et total via `get_device_properties`
                # (gratuit), LIBRE via NVML apparié par bus PCI. On garde l'index TORCH
                # comme `id` — c'est l'espace d'adressage de tous les appelants (`cuda:N`,
                # `gpu.llm_gpu_indices`) ; seule la mesure du libre est empruntée.
                mesure = None
                try:
                    props = torch.cuda.get_device_properties(idx)
                    total = int(props.total_memory)
                    bus = _bus_id(props)
                    lu = _nvml_free_total(bus) if bus is not None else None
                    if lu is not None and _appariement_coherent(lu, total, idx):
                        # Le libre peut dépasser le total PILOTE (NVML compte le matériel) :
                        # on borne, sinon `used` deviendrait négatif chez les appelants.
                        mesure = (min(lu[0], total), total)
                except Exception:  # noqa: BLE001 — doublure de test ou torch réduit → repli
                    mesure = None
                if mesure is None:
                    try:
                        mesure = torch.cuda.mem_get_info(idx)
                    except Exception:  # noqa: BLE001 — carte illisible → ignorée, les saines restent
                        logger.debug("GPU %d illisible — ignoré dans l'inventaire", idx, exc_info=True)
                        continue
                free, total = mesure
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

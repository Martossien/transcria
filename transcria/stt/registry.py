"""Registre unique des moteurs STT natifs (vague C1).

Chaque backend NATIF déclare son ``DESCRIPTOR`` dans son propre module
(``stt/kroko_transcriber.py`` déclare kroko…) : ajouter un backend = 1 module
+ 1 enregistrement dans ``backends()``. Les consommateurs (factory, VRAM,
catalogue de modèles, schéma de config) lisent ce registre — la description
du moteur n'existe qu'ici.

Les backends SERVIS (qwen3asr, nemotron… — runtimes C++ externes, cf.
docs/EXTERNAL_STT_RUNTIMES.md) n'y figurent PAS : ils n'ont pas de builder
local, sont routés par URL (``inference.stt.backends.<nom>``) et leurs poids
vivent dans ``models_catalog._SERVED_STT_SOURCES``. Le plan (§C1) prévoyait
des drapeaux ``experimental``/``remote_only`` : écartés tant qu'aucun backend
enregistré n'en a l'usage (doctrine §9 — l'abstraction naît de son premier
utilisateur, pas d'une spéculation).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from transcria.stt.base_transcriber import BaseTranscriber


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Source Hugging Face d'un backend — sert la page « Modèles » (statut,
    licence, caractère *gated*, estimation de taille)."""

    repo: str
    gated: bool
    license: str
    license_url: str
    est_gb: float


@dataclass(frozen=True)
class SttBackendDescriptor:
    """Description complète d'un backend STT natif.

    ``build(config, device)`` construit le transcriber (import du modèle
    différé dans la classe — la construction ne charge rien) ;
    ``vram_mb(config)`` donne la VRAM locale à réserver (0 = CPU pur) ;
    ``required_model`` est la clé de config d'un chemin de modèle obligatoire
    (garde du schéma), None si le backend se télécharge/résout seul ;
    ``catalog`` est None quand le backend réutilise le modèle d'un autre
    (cohere_tf5 → cohere).
    """

    name: str
    build: Callable[[dict, str | None], BaseTranscriber]
    vram_mb: Callable[[dict], int]
    catalog: ModelCatalogEntry | None
    required_model: str | None = None


def backends() -> dict[str, SttBackendDescriptor]:
    """Table nom → descripteur, dans l'ordre historique de ``_STT_BACKENDS``.

    Reconstruite à chaque appel (imports mis en cache par Python) — pas de
    singleton (§8.1).
    """
    # Différé : évite le cycle backend ↔ registre (chaque module backend
    # importe les dataclasses ci-dessus au chargement).
    from transcria.stt import (
        cohere_tf5_transcriber,
        cohere_transcriber,
        granite_transcriber,
        kroko_transcriber,
        moss_transcriber,
        parakeet_transcriber,
        voxtral_transcriber,
        whisper_transcriber,
    )

    return {
        module.DESCRIPTOR.name: module.DESCRIPTOR
        for module in (
            cohere_transcriber,
            cohere_tf5_transcriber,
            whisper_transcriber,
            granite_transcriber,
            parakeet_transcriber,
            voxtral_transcriber,
            kroko_transcriber,
            moss_transcriber,
        )
    }


def get(name: str) -> SttBackendDescriptor | None:
    return backends().get(name)

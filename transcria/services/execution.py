"""Modes d'exécution typés (vague B0) — remplacent les chaînes libres de l'exécuteur.

Les VALEURS restent les chaînes historiques : elles sont persistées (entrées de file,
``extra_data["execution"]["mode"]``) et comparées côté web — aucune migration de données.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ExecutionMode(Enum):
    PIPELINE = "quality"      # pipeline complet (mode historique par défaut)
    PIPELINE_FAST = "fast"
    SUMMARY = "summary"       # étape GPU : résumé rapide (le runner pose l'état)
    SPEAKER_DETECTION = "speakers"
    REFINEMENT = "refine"

    @property
    def is_step(self) -> bool:
        """Étape GPU synchrone (le runner gère l'état du job) vs pipeline complet."""
        return self in (ExecutionMode.SUMMARY, ExecutionMode.SPEAKER_DETECTION, ExecutionMode.REFINEMENT)

    @classmethod
    def from_string(cls, mode: str) -> "ExecutionMode":
        for member in cls:
            if member.value == mode:
                return member
        # Historique : tout mode inconnu était traité comme un pipeline (fast/quality
        # arrivent aussi via la config) — on garde ce comportement tolérant.
        return cls.PIPELINE


@dataclass(frozen=True)
class ExecutionCommand:
    """Ordre d'exécution complet — remplace le triplet positionnel (job_id, audio, mode)."""

    job_id: str
    mode: ExecutionMode
    audio_path: Path | None = None
    profile_id: str | None = None

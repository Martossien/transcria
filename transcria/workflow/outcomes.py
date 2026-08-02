"""Résultat typé d'une phase/d'un traitement — le contrat inter-couches (vague B0).

Remplace les dictionnaires de forme libre (``{"vram_wait": True}``, ``{"deferred": True}``,
``{"error": …, "step": …}``…) que ``pipeline_service`` et le runner renvoyaient et que
``job_executor`` ré-interprétait clé par clé. Le contrat couvre EXACTEMENT les neuf clés
observées dans le code historique (relevé du plan qualité §3.3) — ni plus, ni moins.

``from_legacy_dict`` est ce qui reste du pont de migration : il encode la PRIORITÉ
historique entre clés (cancelled > deferred > vram_wait > error > succès — figée par
tests/test_job_executor_outcomes_golden.py) et sert au SEUL producteur encore
dictionnaire, le runner d'étape (résumé / détection de locuteurs / affinage).

``to_legacy_dict`` a été retiré : le pipeline renvoie désormais un ``PhaseOutcome``
natif de bout en bout, et plus aucun appelant n'avait besoin de la forme historique.
Le garder aurait entretenu la seconde convention que cette migration devait fermer.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class OutcomeKind(Enum):
    SUCCESS = auto()
    FAILED = auto()
    DEFERRED = auto()       # ressources distantes injoignables (transitoire) → re-file
    WAITING_VRAM = auto()   # VRAM locale insuffisante (transitoire) → re-file
    CANCELLED = auto()


@dataclass(frozen=True)
class PhaseOutcome:
    """Issue d'un traitement (pipeline complet ou étape GPU).

    ``phase`` : où l'issue s'est décidée (ex-clés ``step``/``phase``). ``reason`` : cause
    lisible (ex-clés ``error``/``reason``). ``retry_after_s``/``required_vram_mb`` :
    paramètres de replanification des issues transitoires."""

    kind: OutcomeKind
    phase: str | None = None
    reason: str | None = None
    retry_after_s: int | None = None
    required_vram_mb: int | None = None
    processing_seconds: float | None = None

    @classmethod
    def from_legacy_dict(cls, result: dict) -> PhaseOutcome:
        """Interprète un dict historique — même priorité que l'ancien if/elif de l'exécuteur."""
        phase: str | None = result.get("phase") or result.get("step") or None
        reason: str | None = result.get("error") or result.get("reason") or None
        retry_raw = result.get("retry_after_s")
        retry_after: int | None = int(retry_raw) if retry_raw is not None else None
        processing_raw = result.get("processing_seconds")
        processing: float | None = float(processing_raw) if processing_raw is not None else None

        if result.get("cancelled"):
            kind = OutcomeKind.CANCELLED
        elif result.get("deferred"):
            kind = OutcomeKind.DEFERRED
        elif result.get("vram_wait"):
            kind = OutcomeKind.WAITING_VRAM
        elif result.get("error"):
            kind = OutcomeKind.FAILED
        else:
            kind = OutcomeKind.SUCCESS
        required: int | None = None
        if kind is OutcomeKind.WAITING_VRAM:
            required = int(result.get("required_mb") or 0)
        return cls(
            kind,
            phase=phase,
            reason=reason,
            retry_after_s=retry_after,
            required_vram_mb=required,
            processing_seconds=processing,
        )

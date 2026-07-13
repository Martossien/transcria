"""Tests du contrat PhaseOutcome/ExecutionMode (vague B0) — adaptateurs et priorité.

Le point dur : ``from_legacy_dict`` doit encoder EXACTEMENT la priorité historique de
l'exécuteur (cancelled > deferred > vram_wait > error > succès), et ``to_legacy_dict``
doit ré-émettre une forme que ``from_legacy_dict`` relit à l'identique (aller-retour).
"""
from __future__ import annotations

import pytest

from transcria.services.execution import ExecutionCommand, ExecutionMode
from transcria.workflow.outcomes import OutcomeKind, PhaseOutcome

# Les formes RÉELLEMENT observées dans le code historique (plan qualité §3.3).
OBSERVED_SHAPES = [
    ({"status": "completed", "processing_seconds": 12.5}, OutcomeKind.SUCCESS),
    ({"error": "boom métier", "step": "transcription"}, OutcomeKind.FAILED),
    ({"error": "Traitement annulé", "cancelled": True, "step": "transcription"}, OutcomeKind.CANCELLED),
    ({"deferred": True, "reason": "ressources distantes injoignables", "retry_after_s": 45}, OutcomeKind.DEFERRED),
    ({"vram_wait": True, "required_mb": 6000, "phase": "stt", "retry_after_s": 30}, OutcomeKind.WAITING_VRAM),
    ({"vram_wait": True, "required_mb": 6000, "phase": "stt", "reason": "VRAM insuffisante", "retry_after_s": 30}, OutcomeKind.WAITING_VRAM),
    ({}, OutcomeKind.SUCCESS),  # dict vide = succès (comportement historique du else final)
]


class TestFromLegacy:
    @pytest.mark.parametrize("legacy, kind", OBSERVED_SHAPES)
    def test_observed_shapes(self, legacy, kind):
        assert PhaseOutcome.from_legacy_dict(legacy).kind is kind

    def test_priority_matches_executor(self):
        everything = {"cancelled": True, "deferred": True, "vram_wait": True, "error": "x"}
        assert PhaseOutcome.from_legacy_dict(everything).kind is OutcomeKind.CANCELLED
        assert PhaseOutcome.from_legacy_dict({**everything, "cancelled": False}).kind is OutcomeKind.DEFERRED
        assert PhaseOutcome.from_legacy_dict({"vram_wait": True, "error": "x"}).kind is OutcomeKind.WAITING_VRAM

    def test_fields_extracted(self):
        o = PhaseOutcome.from_legacy_dict(
            {"vram_wait": True, "required_mb": "6000", "phase": "stt", "retry_after_s": "30"}
        )
        assert o.required_vram_mb == 6000 and o.retry_after_s == 30 and o.phase == "stt"
        # `step` (pipeline) et `phase` (vram) sont le même concept
        assert PhaseOutcome.from_legacy_dict({"error": "x", "step": "export"}).phase == "export"


class TestRoundTrip:
    @pytest.mark.parametrize("legacy, kind", OBSERVED_SHAPES)
    def test_to_then_from_is_stable(self, legacy, kind):
        once = PhaseOutcome.from_legacy_dict(legacy)
        again = PhaseOutcome.from_legacy_dict(once.to_legacy_dict())
        assert again == once

    def test_legacy_keys_survive(self):
        d = PhaseOutcome(OutcomeKind.WAITING_VRAM, phase="stt", required_vram_mb=6000, retry_after_s=30).to_legacy_dict()
        assert d["vram_wait"] is True and d["required_mb"] == 6000 and d["phase"] == "stt"
        d = PhaseOutcome(OutcomeKind.FAILED, reason="boom", phase="export").to_legacy_dict()
        assert d["error"] == "boom" and d["step"] == "export"
        d = PhaseOutcome(OutcomeKind.CANCELLED).to_legacy_dict()
        assert d["cancelled"] is True and d["error"]  # l'historique posait toujours un error


class TestExecutionMode:
    def test_values_are_historic_strings(self):
        assert ExecutionMode.SUMMARY.value == "summary"
        assert ExecutionMode.SPEAKER_DETECTION.value == "speakers"
        assert ExecutionMode.REFINEMENT.value == "refine"

    def test_step_detection(self):
        assert ExecutionMode.SUMMARY.is_step and ExecutionMode.REFINEMENT.is_step
        assert not ExecutionMode.PIPELINE.is_step and not ExecutionMode.PIPELINE_FAST.is_step

    def test_unknown_mode_falls_back_to_pipeline(self):
        assert ExecutionMode.from_string("quality") is ExecutionMode.PIPELINE
        assert ExecutionMode.from_string("n_importe_quoi") is ExecutionMode.PIPELINE

    def test_command_is_frozen(self):
        cmd = ExecutionCommand(job_id="j1", mode=ExecutionMode.SUMMARY)
        with pytest.raises(AttributeError):
            cmd.job_id = "j2"  # type: ignore[misc]

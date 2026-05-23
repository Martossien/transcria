"""Tests ciblés JobService."""

from transcria.services.job_service import _quality_summary_from_preflight


def test_quality_summary_from_preflight_exposes_risk_level():
    summary = _quality_summary_from_preflight({"risk_level": "degrade"})

    assert summary == {"diagnostics": {"level": "degrade"}}


def test_quality_summary_from_preflight_ignores_missing_level():
    assert _quality_summary_from_preflight({}) == {}
    assert _quality_summary_from_preflight({"flags": ["audio_faible"]}) == {}

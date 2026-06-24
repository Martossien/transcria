"""Phase 7 — contrôle qualité léger (light_report)."""
from __future__ import annotations

import tempfile

import pytest

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.quality.light_report import run_light_quality


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _job(jid="job-light"):
    return Job(id=jid, owner_id="u1", title="Light", state=JobState.CREATED.value)


def test_srt_propre_score_eleve_et_schema_compatible(tmp_dir):
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    fs.save_json("metadata/transcription_segments.json", [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])

    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

    # Schéma compatible avec le rapport complet (clés attendues par l'UI) + marqueur léger.
    assert set(report) >= {"total_checks", "warnings", "checks", "review_points", "review_load", "quality_score"}
    assert report["level"] == "light"
    assert report["quality_score"] == 100
    assert report["warnings"] == 0
    # Fichiers écrits.
    assert fs.load_json("quality/quality_report.json")["level"] == "light"
    assert fs.load_json("quality/review_points.json") == []


def test_sans_srt_score_zero(tmp_dir):
    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})
    assert report["quality_score"] == 0
    assert any(c["type"] == "missing_srt" for c in report["checks"])


def test_segments_vides_et_courts_penalisent(tmp_dir):
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nok\n")
    fs.save_json("metadata/transcription_segments.json", [
        {"start": 1.0, "end": 4.0, "text": "ok"},
        {"start": 4.0, "end": 4.1, "text": "x"},   # très court (<0.5s)
        {"start": 5.0, "end": 5.0, "text": ""},     # vide
    ])
    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})
    assert report["warnings"] >= 2
    assert report["quality_score"] < 100
    types = {c["type"] for c in report["checks"]}
    assert "empty_segments" in types and "short_segments" in types

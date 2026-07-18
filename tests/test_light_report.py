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


def test_trous_de_transcription_marques_releves(tmp_dir):
    """Garde-fou §4.1 : un `transcription_gap_before_s` posé par le backend (MOSS)
    devient un avertissement + point de revue avec position du pire trou."""
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    fs.save_json("metadata/transcription_segments.json", [
        {"start": 1.0, "end": 4.0, "text": "Bonjour"},
        {"start": 26.0, "end": 30.0, "text": "suite", "transcription_gap_before_s": 22.0},
        {"start": 45.0, "end": 50.0, "text": "fin", "transcription_gap_before_s": 15.0},
    ])

    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

    gap_checks = [c for c in report["checks"] if c["type"] == "transcription_gaps"]
    assert gap_checks == [{"type": "transcription_gaps", "count": 2,
                           "max_gap_s": 22.0, "severity": "warning"}]
    assert any("22" in p and "00:26" in p for p in report["review_points"])
    assert report["quality_score"] < 100


def test_trous_naturels_sans_marqueur_ignores(tmp_dir):
    """Défaut inchangé : un simple silence entre segments (sans marqueur backend)
    ne déclenche RIEN — pas de faux positif sur les autres moteurs/VAD."""
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    fs.save_json("metadata/transcription_segments.json", [
        {"start": 1.0, "end": 4.0, "text": "Bonjour"},
        {"start": 90.0, "end": 95.0, "text": "beaucoup plus tard"},
    ])

    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})
    assert not any(c["type"] == "transcription_gaps" for c in report["checks"])
    assert report["quality_score"] == 100


def test_fin_tronquee_moss_alerte_et_plafonne_le_score(tmp_dir):
    """Défense §4.1 : metadata/moss.json présent + fin d'audio jamais transcrite
    → avertissement « fin tronquée » et score plafonné."""
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    fs.save_json("metadata/transcription_segments.json",
                 [{"start": 1.0, "end": 1053.0, "text": "dernier segment"}])
    fs.save_json("metadata/moss.json", {"backend": "moss"})
    fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 1200.0})

    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

    assert any(c["type"] == "truncated_tail" for c in report["checks"])
    assert any("17:33" in p and "20:00" in p for p in report["review_points"])
    assert report["quality_score"] <= 40


def test_fin_silencieuse_sans_moss_pas_d_alerte(tmp_dir):
    """Défaut inchangé : sans metadata/moss.json, une réunion finissant en
    silence (autres backends, VAD) ne déclenche rien."""
    fs = JobFilesystem(tmp_dir, "job-light")
    fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    fs.save_json("metadata/transcription_segments.json",
                 [{"start": 1.0, "end": 1053.0, "text": "dernier segment"}])
    fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 1200.0})

    report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})
    assert not any(c["type"] == "truncated_tail" for c in report["checks"])

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


class TestPassesLlmMuettes:
    """Une passe LLM qui ne rend RIEN doit se DIRE, pas se deviner.

    Vécu le 2026-08-23 : le moteur d'arbitrage est mort en cours de parcours. Le job
    s'est terminé « avec succès », correction et relecture toutes deux muettes, et le
    rapport ne listait que les conséquences (variantes non résolues) — jamais la cause.
    """

    def test_correction_sans_aucune_modification_leve_un_point(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "job-light")
        srt = "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n"
        fs.save_text("metadata/transcription.srt", srt)
        fs.save_text("metadata/transcription_corrigee.srt", srt)
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

        assert any(c["type"] == "silent_correction" for c in report["checks"])
        assert any("correction" in p.lower() for p in report["review_points"])

    def test_correction_qui_a_modifie_ne_leve_rien(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "job-light")
        fs.save_text("metadata/transcription.srt",
                     "1\n00:00:01,000 --> 00:00:04,000\nbonjour\n")
        fs.save_text("metadata/transcription_corrigee.srt",
                     "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

        assert not any(c["type"] == "silent_correction" for c in report["checks"])

    def test_relecture_muette_leve_un_point_quand_il_y_avait_de_quoi_travailler(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "job-light")
        fs.save_text("metadata/transcription.srt",
                     "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])
        fs.save_json("context/meeting_context.json", {"summary_llm": "## Synthèse\n\nUn texte."})

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

        assert any(c["type"] == "silent_final_review" for c in report["checks"])

    def test_sans_synthese_amont_la_relecture_muette_est_normale(self, tmp_dir):
        """Ne rien produire quand il n'y avait rien à harmoniser n'est pas une panne."""
        fs = JobFilesystem(tmp_dir, "job-light")
        fs.save_text("metadata/transcription.srt",
                     "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}})

        assert not any(c["type"] == "silent_final_review" for c in report["checks"])

    def test_un_profil_sans_correction_ne_leve_jamais_ces_points(self, tmp_dir):
        """Cinq profils légers ne lancent ni correction ni relecture : un SRT « identique »
        et une harmonisation absente y sont l'état NORMAL, pas une panne. Sans la garde,
        chaque livrable srt_express/word_rapide portait un faux avertissement."""
        from transcria.workflow.profiles import get_profile

        fs = JobFilesystem(tmp_dir, "job-light")
        srt = "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n"
        fs.save_text("metadata/transcription.srt", srt)
        fs.save_text("metadata/transcription_corrigee.srt", srt)
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])
        fs.save_json("context/meeting_context.json", {"summary_llm": "## Synthèse\n\nTexte."})

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}},
                                   profile=get_profile("word_rapide"))

        types = {c["type"] for c in report["checks"]}
        assert "silent_correction" not in types
        assert "silent_final_review" not in types

    def test_le_profil_word_corrige_garde_les_deux_controles(self, tmp_dir):
        from transcria.workflow.profiles import get_profile

        fs = JobFilesystem(tmp_dir, "job-light")
        srt = "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n"
        fs.save_text("metadata/transcription.srt", srt)
        fs.save_text("metadata/transcription_corrigee.srt", srt)
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 1.0, "end": 4.0, "text": "Bonjour"}])
        fs.save_json("context/meeting_context.json", {"summary_llm": "## Synthèse\n\nTexte."})

        report = run_light_quality(_job(), {"storage": {"jobs_dir": tmp_dir}},
                                   profile=get_profile("word_corrige"))

        types = {c["type"] for c in report["checks"]}
        assert {"silent_correction", "silent_final_review"} <= types


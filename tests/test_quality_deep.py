"""Tests approfondis pour le module qualité avec SRT réel."""
import tempfile
from pathlib import Path

import pytest

from transcria.quality.srt_checks import SRTChecker
from transcria.quality.lexicon_checks import LexiconChecker
from transcria.quality.review_points import ReviewPoints
from transcria.quality.quality_report import QualityReporter
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState


REAL_SRT = """1
00:00:00,000 --> 00:00:05,000
Bonjour à tous, merci d'être présents.

2
00:00:05,000 --> 00:00:12,500
Aujourd'hui nous allons parler du projet API Gateway et du déploiement Kubernetes.

3
00:00:12,500 --> 00:00:20,000
Sophie Martin va nous présenter l'avancement du sprint 12.

4
00:00:20,000 --> 00:00:35,000
Thomas Dubois a travaillé sur les microservices et le CI/CD pipeline.

5
00:00:35,000 --> 00:00:50,000
Marie Leroy va détailler le backlog et les user stories prioritaires.

6
00:00:50,000 --> 00:01:05,000
Karim Bensaid présente les maquettes UX pour le MVP.

7
00:01:05,000 --> 00:01:15,000
Nous devons finaliser le sprint 12 avant vendredi.

8
00:01:15,000 --> 00:01:30,000
La rétrospective est prévue jeudi matin.

9
00:01:30,000 --> 00:01:45,000
Merci à tous pour votre participation.

"""

SRT_WITH_GAPS = """1
00:00:00,000 --> 00:00:10,000
Premier segment.

2
00:00:30,000 --> 00:00:40,000
Deuxième segment après un trou de 20 secondes.

3
00:00:40,000 --> 00:00:50,000
Troisième segment.
"""

SRT_WITH_OVERLAPS = """1
00:00:00,000 --> 00:00:15,000
Premier segment.

2
00:00:10,000 --> 00:00:25,000
Chevauchement avec le premier.

3
00:00:20,000 --> 00:00:30,000
Chevauchement avec le deuxième.
"""

SRT_EMPTY_SEGMENTS = """1
00:00:00,000 --> 00:00:10,000
Contenu.

2
00:00:10,000 --> 00:00:12,000

3
00:00:12,000 --> 00:00:20,000
Encore du contenu.
"""


def _parse_srt(srt_text: str) -> list[dict]:
    segments = []
    for block in srt_text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            times = lines[1].split(" --> ")
            text = " ".join(lines[2:])
            segments.append({
                "start": _ts_to_sec(times[0]),
                "end": _ts_to_sec(times[1]),
                "text": text,
            })
    return segments


def _ts_to_sec(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


class TestSRTCheckerReal:
    def test_good_srt_passes_all(self):
        segments = _parse_srt(REAL_SRT)
        result = SRTChecker.check_segments(segments)
        assert result["total"] == 9
        assert result["clean_count"] == 9

    def test_detects_gaps(self):
        segments = _parse_srt(SRT_WITH_GAPS)
        result = SRTChecker.check_segments(segments)
        assert result["total"] == 3

    def test_detects_overlaps_in_raw(self):
        segments = _parse_srt(SRT_WITH_OVERLAPS)
        issues = SRTChecker.check_segments(segments)
        assert issues["total"] == 3

    def test_detects_empty_segments(self):
        segments = [
            {"start": 0, "end": 10, "text": "Contenu."},
            {"start": 10, "end": 12, "text": ""},
            {"start": 12, "end": 20, "text": "Encore du contenu."},
        ]
        result = SRTChecker.check_segments(segments)
        assert result["total"] == 3
        assert result["clean_count"] == 2
        assert len(result["issues"]) == 1

    def test_boundary_zero_duration(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 0, "text": "?"})
        assert len(issues) > 0

    def test_boundary_max_duration(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 119, "text": "x"})
        assert len(issues) == 0

    def test_boundary_over_max(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 121, "text": "x"})
        assert len(issues) > 0

    def test_negative_timestamps(self):
        issues = SRTChecker.check_segment({"start": -1, "end": 5, "text": "x"})
        assert len(issues) == 0

    def test_very_large_timestamps(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 99999, "text": "x"})
        assert len(issues) > 0


class TestLexiconCheckerReal:
    def test_full_match_on_real_srt(self):
        lexicon = [
            {"term": "API Gateway", "variants": ["gateway"]},
            {"term": "Kubernetes", "variants": ["k8s"]},
            {"term": "sprint 12", "variants": ["sprint douze"]},
            {"term": "Sophie Martin", "variants": []},
            {"term": "Thomas Dubois", "variants": ["Thomas"]},
            {"term": "Marie Leroy", "variants": ["Marie"]},
            {"term": "Karim Bensaid", "variants": ["Karim"]},
            {"term": "MVP", "variants": []},
            {"term": "CI/CD", "variants": ["CICD"]},
            {"term": "backlog", "variants": []},
            {"term": "user story", "variants": ["US", "stories"]},
        ]
        result = LexiconChecker.check(REAL_SRT, lexicon)
        assert "API Gateway" in result["found"]
        assert "Kubernetes" in result["found"]
        assert "sprint 12" in result["found"]
        assert "Sophie Martin" in result["found"]
        assert "MVP" in result["found"]
        assert "CI/CD" in result["found"]
        assert "backlog" in result["found"]
        assert len(result["found"]) >= 8
        assert len(result["missing"]) <= 3

    def test_variant_detection(self):
        lexicon = [{"term": "ORGANISATION", "variants": ["ORG", "ORGA"]}]
        result = LexiconChecker.check("L'ORG est présente", lexicon)
        assert len(result["variants_found"]) == 1
        assert result["variants_found"][0]["variant"] == "ORG"

    def test_no_false_positive_partial_match(self):
        lexicon = [{"term": "ORGANISATION", "variants": []}]
        result = LexiconChecker.check("Il faut s'organiser pour le projet", lexicon)
        assert "ORGANISATION" not in result["found"]

    def test_accent_insensitive(self):
        lexicon = [{"term": "rétrospective", "variants": []}]
        result = LexiconChecker.check("La retrospective est jeudi", lexicon)
        assert "rétrospective" not in result["found"]


class TestQualityReportIntegration:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_report_on_real_srt(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-1")
        fs.save_text("metadata/transcription.srt", REAL_SRT)
        fs.save_json("metadata/transcription_segments.json", _parse_srt(REAL_SRT))
        fs.save_json("metadata/audio_analysis.json", {
            "duration_seconds": 105,
            "format": "mp3", "codec": "mp3", "channels": 1, "sample_rate_hz": 16000,
        })
        fs.save_json("context/session_lexicon.json", [
            {"term": "API Gateway", "category": "technique", "priority": "critique"},
            {"term": "Kubernetes", "category": "technique", "priority": "normale"},
            {"term": "MVP", "category": "sigle", "priority": "critique"},
            {"term": "TERME_ABSENT", "category": "autre", "priority": "normale"},
        ])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-1", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        assert report["quality_score"] > 0
        assert report["total_checks"] > 0
        assert "API Gateway" not in str(report.get("review_points", []))
        missing_check = [c for c in report["checks"] if c["type"] == "missing_lexicon_terms"]
        if missing_check:
            assert "TERME_ABSENT" in missing_check[0]["terms"]

    def test_report_on_empty_srt(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-empty")
        fs.save_text("metadata/transcription.srt", "")
        fs.save_json("metadata/transcription_segments.json", [])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 100})

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-empty", owner_id="u1", title="Empty", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        assert "low_coverage" in [c["type"] for c in report["checks"]]
        assert report["quality_score"] < 100

    def test_report_flags_unresolved_lexicon_variants_after_correction(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-lexicon")
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:03,000\n"
            "SPEAKER_00: Le Terme suspect reste dans le texte.\n\n"
            "2\n"
            "00:00:03,000 --> 00:00:06,000\n"
            "SPEAKER_00: Un Element reste aussi dans le texte.\n"
        )
        fs.save_text("metadata/transcription.srt", srt)
        fs.save_text("metadata/transcription_corrigee.srt", srt)
        fs.save_json("metadata/transcription_segments.json", _parse_srt(srt))
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 6})
        fs.save_json("context/session_lexicon.json", [
            {"term": "Terme validé", "variants": ["Terme suspect"]},
            {"term": "Élément", "variants": ["Elementt"]},
        ])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-lexicon", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        check = [c for c in report["checks"] if c["type"] == "unresolved_lexicon_variants"]
        assert check
        assert check[0]["count"] == 2
        assert "Terme suspect" in str(report["review_points"])
        assert "Element proche de Élément" in str(report["review_points"])

    def test_micro_overlaps_are_reported_but_not_over_penalized(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-overlaps")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json", [
            {"start": 0.0, "end": 2.0, "text": "A"},
            {"start": 1.8, "end": 3.0, "text": "B"},
            {"start": 2.7, "end": 4.0, "text": "C"},
        ])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 4})
        fs.save_json("context/session_lexicon.json", [])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-overlaps", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        overlap_check = [c for c in report["checks"] if c["type"] == "overlaps"][0]
        assert overlap_check["count"] == 2
        assert overlap_check["significant_count"] == 0
        assert overlap_check["severity"] == "info"
        assert report["quality_score"] == 100

    def test_asr_noise_markers_are_configurable(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-noise-markers")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:00,700\nmarqueur test\n")
        fs.save_json("metadata/transcription_segments.json", [
            {"start": 0.0, "end": 0.7, "text": "marqueur test"},
        ])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 1})
        fs.save_json("context/session_lexicon.json", [])

        reporter = QualityReporter({
            "storage": {"jobs_dir": tmp_dir},
            "quality": {"asr_noise_markers": ["marqueur test"]},
        })
        job = Job(id="test-q-noise-markers", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        checks = [c for c in report["checks"] if c["type"] == "suspicious_short_segments"]
        assert checks
        assert report["review_load"]["suspicious_short_segments"] == 1

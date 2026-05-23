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

    def test_audio_scene_problem_segments_are_reported(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-audio-scene")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json", [
            {"start": 0.0, "end": 2.0, "text": "Bonjour"},
        ])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 20})
        fs.save_json("metadata/audio_scene.json", {
            "problem_segments": [
                {"label": "noise", "start": 12.0, "end": 18.5, "duration_s": 6.5},
                {"label": "music", "start": 60.0, "end": 72.0, "duration_s": 12.0},
            ],
        })
        fs.save_json("context/session_lexicon.json", [])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-audio-scene", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        checks = [c for c in report["checks"] if c["type"] == "audio_problem_segments"]
        assert checks
        assert checks[0]["count"] == 2
        assert checks[0]["examples"][0]["label"] == "bruit"
        assert checks[0]["examples"][0]["start_label"] == "00:12"
        assert "Zones audio problématiques : 2" in str(report["review_points"])
        assert report["review_load"]["audio_problem_segments"] == 2

    def test_audio_preflight_flags_are_reported(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-audio-preflight")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json", [
            {"start": 0.0, "end": 2.0, "text": "Bonjour"},
        ])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 20})
        fs.save_json("metadata/audio_preflight.json", {
            "risk_level": "degrade",
            "rms": 0.006,
            "estimated_snr_db": 4.2,
            "bandwidth_95_hz": 3200.0,
            "silence_ratio": 0.37,
            "flags": ["audio_tres_faible", "risque_transcription_non_fiable"],
        })
        fs.save_json("context/session_lexicon.json", [])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-audio-preflight", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        checks = [c for c in report["checks"] if c["type"] == "audio_preflight_flags"]
        assert checks
        assert checks[0]["risk_level"] == "degrade"
        assert checks[0]["metrics"]["rms"] == pytest.approx(0.006)
        assert "Pré-diagnostic audio" in str(report["review_points"])
        assert report["review_load"]["audio_preflight_flags"] == 2
        md = fs.load_text("quality/quality_report.md") or ""
        assert "## Diagnostic audio avant transcription" in md
        assert "audio_tres_faible" in md

    def test_segment_reliability_counts_are_reported(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "test-q-segment-reliability")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
        fs.save_json("metadata/transcription_segments.json", [
            {
                "start": 0.0,
                "end": 0.2,
                "text": "Bonjour",
                "reliability": "degrade",
                "reliability_reasons": ["segment_micro", "audio_preflight_degrade"],
            },
            {"start": 1.0, "end": 2.0, "text": "suite", "reliability": "ok", "reliability_reasons": []},
        ])
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 20})
        fs.save_json("context/session_lexicon.json", [])

        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="test-q-segment-reliability", owner_id="u1", title="Test", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)

        checks = [c for c in report["checks"] if c["type"] == "segment_reliability"]
        assert checks
        assert checks[0]["counts"]["degrade"] == 1
        assert report["review_load"]["degraded_reliability_segments"] == 1


class TestASRNoiseLooksLike:
    """Tests unitaires pour _looks_like_asr_noise : normalisation et marqueurs par défaut."""

    def _reporter(self, markers=None):
        cfg = {} if markers is None else {"quality": {"asr_noise_markers": markers}}
        return QualityReporter(cfg)

    def test_punctuated_marker_normalized_and_detected(self):
        """Un marqueur suivi d'une ponctuation finale est normalisé avant comparaison."""
        r = self._reporter(["music"])
        assert r._looks_like_asr_noise("Music.")

    def test_bracketed_marker_normalized_and_detected(self):
        """Un marqueur entre crochets (ex. [Music]) est normalisé et détecté."""
        r = self._reporter(["music"])
        assert r._looks_like_asr_noise("[Music]")

    def test_exclamation_stripped_before_match(self):
        """Un point d'exclamation final n'empêche pas la détection du marqueur."""
        r = self._reporter(["applause"])
        assert r._looks_like_asr_noise("Applause!")

    def test_youtube_outro_english_in_default_config(self):
        """'Thanks for watching' est détecté avec la configuration par défaut."""
        from transcria.config.loader import get_default_config
        r = QualityReporter(get_default_config())
        assert r._looks_like_asr_noise("Thanks for watching.")

    def test_amara_credit_in_default_config(self):
        """Service tiers 'rev.com' est dans les marqueurs par défaut."""
        from transcria.config.loader import get_default_config
        r = QualityReporter(get_default_config())
        assert r._looks_like_asr_noise("rev.com")

    def test_legitimate_french_phrase_not_flagged(self):
        """Phrase courante de réunion française n'est pas classée bruit ASR."""
        from transcria.config.loader import get_default_config
        r = QualityReporter(get_default_config())
        assert not r._looks_like_asr_noise("Merci pour votre contribution.")


class TestSuspectNoSpeechProb:
    """Détection des segments à haute probabilité de non-parole (bench test5.wav)."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def _base_fs(self, tmp_dir, job_id, segments):
        fs = JobFilesystem(tmp_dir, job_id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 10})
        fs.save_json("context/session_lexicon.json", [])
        return fs

    def test_high_no_speech_prob_above_threshold_is_flagged(self, tmp_dir):
        """Un segment avec no_speech_prob > 0.5 génère un check suspect_no_speech_prob."""
        segs = [{"start": 0.8, "end": 2.5, "text": "La force dans la médecine, c'est juste...",
                 "no_speech_prob": 0.60}]
        self._base_fs(tmp_dir, "j-nsp-1", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-nsp-1", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_no_speech_prob" in check_types

    def test_low_no_speech_prob_below_threshold_is_clean(self, tmp_dir):
        """Un segment avec no_speech_prob = 0.1 ne génère pas de check suspect."""
        segs = [{"start": 0.0, "end": 2.0, "text": "Bonjour à tous.", "no_speech_prob": 0.10}]
        self._base_fs(tmp_dir, "j-nsp-2", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-nsp-2", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_no_speech_prob" not in check_types

    def test_segments_without_no_speech_prob_field_are_ignored(self, tmp_dir):
        """Les segments sans champ no_speech_prob (Cohere) ne lèvent pas de fausse alarme."""
        segs = [{"start": 0.0, "end": 2.0, "text": "Bonjour."}]
        self._base_fs(tmp_dir, "j-nsp-3", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-nsp-3", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_no_speech_prob" not in check_types

    def test_no_speech_prob_threshold_configurable(self, tmp_dir):
        """Le seuil de détection no_speech_prob est configurable (ici abaissé à 0.3)."""
        segs = [{"start": 0.0, "end": 2.0, "text": "C'est un fort constance.",
                 "no_speech_prob": 0.37}]
        self._base_fs(tmp_dir, "j-nsp-4", segs)
        reporter = QualityReporter({
            "storage": {"jobs_dir": tmp_dir},
            "quality": {"thresholds": {"no_speech_prob_threshold": 0.3}},
        })
        job = Job(id="j-nsp-4", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_no_speech_prob" in check_types

    def test_suspect_no_speech_count_and_examples_present(self, tmp_dir):
        """Le check expose le nombre de segments suspects et des exemples."""
        segs = [
            {"start": 0.8, "end": 2.5, "text": "La force dans la médecine.", "no_speech_prob": 0.60},
            {"start": 3.0, "end": 5.0, "text": "C'est bon.", "no_speech_prob": 0.55},
            {"start": 6.0, "end": 8.0, "text": "Bonjour.", "no_speech_prob": 0.10},
        ]
        self._base_fs(tmp_dir, "j-nsp-5", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-nsp-5", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check = [c for c in report["checks"] if c["type"] == "suspect_no_speech_prob"][0]
        assert check["count"] == 2
        assert len(check["examples"]) == 2


class TestSuspectLowWordConfidence:
    """Détection des segments où la majorité des mots ont une faible confiance STT."""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def _base_fs(self, tmp_dir, job_id, segments):
        fs = JobFilesystem(tmp_dir, job_id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")
        fs.save_json("metadata/transcription_segments.json", segments)
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 10})
        fs.save_json("context/session_lexicon.json", [])
        return fs

    def _words(self, probs: list[float], text: str = "") -> list[dict]:
        words = text.split() if text else [f"mot{i}" for i in range(len(probs))]
        return [{"word": w, "probability": p} for w, p in zip(words, probs)]

    def test_majority_low_confidence_words_flagged(self, tmp_dir):
        """Un segment avec >50% de mots à faible confiance génère suspect_low_word_confidence."""
        # 4/6 mots sous 0.4 → ratio = 0.67 > 0.5
        segs = [{
            "start": 0.8, "end": 2.5,
            "text": "La force dans la médecine c'est",
            "words": self._words([0.03, 0.08, 0.12, 0.05, 0.50, 0.88], "La force dans la médecine c'est"),
        }]
        self._base_fs(tmp_dir, "j-lwc-1", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-lwc-1", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_low_word_confidence" in check_types

    def test_high_confidence_words_not_flagged(self, tmp_dir):
        """Un segment dont tous les mots ont confiance > 0.7 n'est pas signalé."""
        segs = [{
            "start": 0.0, "end": 2.0,
            "text": "Bonjour à tous les participants",
            "words": self._words([0.99, 0.95, 0.88, 0.91, 0.97]),
        }]
        self._base_fs(tmp_dir, "j-lwc-2", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-lwc-2", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_low_word_confidence" not in check_types

    def test_segments_without_words_field_are_ignored(self, tmp_dir):
        """Les segments sans champ words ne lèvent pas de fausse alarme."""
        segs = [{"start": 0.0, "end": 2.0, "text": "Bonjour."}]
        self._base_fs(tmp_dir, "j-lwc-3", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-lwc-3", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_low_word_confidence" not in check_types

    def test_word_confidence_thresholds_configurable(self, tmp_dir):
        """Les seuils de confiance sont configurables depuis quality.thresholds."""
        # 1/4 mot sous 0.4 = ratio 0.25 — sous le défaut 0.5 mais au-dessus d'un seuil 0.2
        segs = [{
            "start": 0.0, "end": 2.0,
            "text": "Bonjour à tous ici",
            "words": self._words([0.03, 0.92, 0.88, 0.95]),
        }]
        self._base_fs(tmp_dir, "j-lwc-4", segs)
        reporter = QualityReporter({
            "storage": {"jobs_dir": tmp_dir},
            "quality": {"thresholds": {"low_word_confidence_ratio": 0.2}},
        })
        job = Job(id="j-lwc-4", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check_types = [c["type"] for c in report["checks"]]
        assert "suspect_low_word_confidence" in check_types

    def test_suspect_low_confidence_count_and_examples(self, tmp_dir):
        """Le check expose le nombre de segments et les exemples avec ratio calculé."""
        segs = [
            {
                "start": 0.8, "end": 2.5, "text": "La force dans la médecine.",
                # 3/5 mots < 0.4 → ratio 0.60 > 0.5
                "words": self._words([0.03, 0.88, 0.12, 0.08, 0.90]),
            },
            {
                "start": 3.0, "end": 5.0, "text": "Bonjour ici.",
                "words": self._words([0.99, 0.95]),
            },
        ]
        self._base_fs(tmp_dir, "j-lwc-5", segs)
        reporter = QualityReporter({"storage": {"jobs_dir": tmp_dir}})
        job = Job(id="j-lwc-5", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value)
        report = reporter.run_all_checks(job)
        check = [c for c in report["checks"] if c["type"] == "suspect_low_word_confidence"][0]
        assert check["count"] == 1
        assert check["examples"][0]["low_conf_ratio"] > 0.5

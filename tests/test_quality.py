import pytest

from transcria.quality.srt_checks import SRTChecker
from transcria.quality.lexicon_checks import LexiconChecker
from transcria.quality.review_points import ReviewPoints


class TestSRTChecker:
    def test_check_empty_segment(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 5, "text": ""})
        assert any("vide" in i.lower() for i in issues)

    def test_check_too_short(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 0.05, "text": "hi"})
        assert any("court" in i.lower() for i in issues)

    def test_check_too_long(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 150, "text": "a" * 10})
        assert any("long" in i.lower() for i in issues)

    def test_check_good_segment(self):
        issues = SRTChecker.check_segment({"start": 0, "end": 5, "text": "Hello world"})
        assert len(issues) == 0

    def test_check_inverted_timestamps(self):
        issues = SRTChecker.check_segment({"start": 10, "end": 5, "text": "x"})
        assert any("invers" in i.lower() for i in issues)

    def test_check_segments_stats(self):
        segments = [
            {"start": 0, "end": 5, "text": "ok"},
            {"start": 5, "end": 6, "text": ""},
            {"start": 6, "end": 6.1, "text": "short"},
        ]
        result = SRTChecker.check_segments(segments)
        assert result["total"] == 3
        assert result["clean_count"] == 1
        assert len(result["issues"]) == 2


class TestLexiconChecker:
    def test_finds_present_terms(self):
        lexicon = [{"term": "ORG-ALPHA", "variants": []}, {"term": "Dr Dupont", "variants": []}]
        result = LexiconChecker.check("Bonjour ORG-ALPHA, ici Dr Dupont", lexicon)
        assert "ORG-ALPHA" in result["found"]
        assert "Dr Dupont" in result["found"]
        assert len(result["missing"]) == 0

    def test_finds_missing_terms(self):
        lexicon = [{"term": "ORG-ALPHA", "variants": []}, {"term": "MISSING-TERM", "variants": []}]
        result = LexiconChecker.check("Bonjour ORG-ALPHA", lexicon)
        assert "ORG-ALPHA" in result["found"]
        assert "MISSING-TERM" in result["missing"]

    def test_case_insensitive(self):
        lexicon = [{"term": "TestTerm", "variants": []}]
        result = LexiconChecker.check("testterm used here", lexicon)
        assert "TestTerm" in result["found"]

    def test_detects_variants(self):
        lexicon = [{"term": "ORGANISATION", "variants": ["ORG", "ORGA"]}]
        result = LexiconChecker.check("ORG est là", lexicon)
        assert len(result["variants_found"]) == 1
        assert result["variants_found"][0]["variant"] == "ORG"
        assert result["variants_found"][0]["canonical"] == "ORGANISATION"

    def test_detects_unresolved_exact_variant(self):
        lexicon = [{"term": "Terme validé", "variants": ["Terme suspect"]}]
        result = LexiconChecker.find_unresolved_terms("Le Terme suspect reste présent.", lexicon)
        assert result["exact_variants"] == [{"term": "Terme validé", "variant": "Terme suspect"}]

    def test_detects_unresolved_close_accent_form(self):
        lexicon = [{"term": "Élément", "variants": ["Elementt"]}]
        result = LexiconChecker.find_unresolved_terms("Un Element reste présent.", lexicon)
        assert result["close_forms"] == [{"term": "Élément", "form": "Element"}]

    def test_ignores_case_only_close_form(self):
        lexicon = [{"term": "Élément", "variants": ["Elementt"]}]
        result = LexiconChecker.find_unresolved_terms("Un élément reste présent.", lexicon)
        assert result["close_forms"] == []

    def test_empty_lexicon(self):
        result = LexiconChecker.check("some text", [])
        assert result["found"] == []
        assert result["missing"] == []
        assert result["variants_found"] == []


class TestReviewPoints:
    def test_generates_from_report(self):
        report = {
            "quality_score": 80,
            "checks": [
                {"type": "empty_segments", "count": 2, "severity": "warning"},
                {"type": "missing_lexicon_terms", "terms": ["TERM1", "TERM2"], "severity": "warning"},
            ],
        }
        points = ReviewPoints.generate(report)
        assert len(points) == 2
        assert any("vide" in p.lower() for p in points)
        assert any("TERM1" in p for p in points)

    def test_handles_overlaps(self):
        report = {"checks": [{"type": "overlaps", "count": 5, "severity": "warning"}]}
        points = ReviewPoints.generate(report)
        assert len(points) == 1
        assert "Chevauchement" in points[0]

    def test_handles_low_coverage(self):
        report = {"checks": [{"type": "low_coverage", "ratio": 0.5, "severity": "error"}]}
        points = ReviewPoints.generate(report)
        assert len(points) == 1
        assert "50%" in points[0]

    def test_handles_unresolved_lexicon_variants(self):
        report = {"checks": [{
            "type": "unresolved_lexicon_variants",
            "exact_variants": [{"term": "Terme validé", "variant": "Terme suspect"}],
            "close_forms": [{"term": "Élément", "form": "Element"}],
            "severity": "warning",
        }]}
        points = ReviewPoints.generate(report)
        assert len(points) == 1
        assert "Terme suspect" in points[0]
        assert "Element proche de Élément" in points[0]

"""Annotations de doute de transcription laissées EN LIGNE dans la synthèse — vécu 2026-11-25
(job réel, LLM cloud) : 25 parenthèses « (transcrit « X », probablement Y) » retirées à la main.

Trois défenses : le prompt de résumé les interdit (v2.10, six langues), la relecture finale les
retire (v1.1), et le rapport qualité DIT celles qui restent dans la synthèse effective.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from transcria.quality.quality_report import QualityReporter
from transcria.quality.review_points import ReviewPoints

ROOT = Path(__file__).resolve().parents[1] / "configs" / "prompts"
LANGS = (".", "en", "de", "es", "it", "pt")


class TestDetectionPure:
    def test_les_parentheses_de_doute_sont_comptees(self):
        text = ("Le projet Alfa (transcrit « Alpha ») avance. La forme retenue (probablement Outil B, "
                "à valider) reste à confirmer. Le budget (en milliers d'euros) est tenu.")
        found = QualityReporter.find_doubt_annotations(text)
        assert found == ["(transcrit « Alpha »)", "(probablement Outil B, à valider)"]

    @pytest.mark.parametrize("text", [
        "The tool (transcribed \"Alfa\", probably Alpha) is used.",
        "Das Werkzeug (transkribiert „Alfa“) wird genutzt.",
        "La herramienta (transcrito «Alfa», probablemente Alpha) se usa.",
        "Lo strumento (trascritto «Alfa») è usato.",
        "A ferramenta (provavelmente Alpha, a validar) é usada.",
    ])
    def test_six_langues(self, text):
        assert len(QualityReporter.find_doubt_annotations(text)) == 1

    def test_les_parentheses_ordinaires_ne_comptent_pas(self):
        assert QualityReporter.find_doubt_annotations("Le CSE (comité) a voté (4 voix pour).") == []
        assert QualityReporter.find_doubt_annotations("") == []


def test_le_rapport_qualite_dit_les_annotations_restantes(tmp_path):
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.models import Job, JobState

    srt = "1\n00:00:00,000 --> 00:00:04,000\nSPEAKER_00(Alice): bonjour à tous.\n"
    fs = JobFilesystem(str(tmp_path), "job-a")
    fs.save_text("metadata/transcription.srt", srt)
    fs.save_text("metadata/transcription_corrigee.srt", srt)
    fs.save_json("metadata/transcription_segments.json", [
        {"start": 0.0, "end": 4.0, "speaker": "Alice", "text": "bonjour à tous."}])
    fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 4, "format": "wav", "codec": "pcm",
                                                   "channels": 1, "sample_rate_hz": 16000})
    # La synthèse ÉDITÉE prime : c'est elle qui est livrée, c'est elle qui est contrôlée.
    fs.save_json("context/meeting_context.json", {
        "summary_llm": "Synthèse LLM propre.",
        "summary": "L'outil (transcrit « Outil Bé ») est retenu (probablement en version 2, à valider).",
    })
    report = QualityReporter({"storage": {"jobs_dir": str(tmp_path)}}).run_all_checks(
        Job(id="job-a", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value))
    check = next(c for c in report["checks"] if c["type"] == "synthesis_inline_annotations")
    assert check["count"] == 2 and check["severity"] == "warning"
    point = next(p for p in report["review_points"] if p.startswith("Synthèse : 2 annotation(s)"))
    assert "(transcrit « Outil Bé »)" in point
    # Le générateur de points de relecture (consommé par l'affinage) les porte aussi.
    assert any("Synthèse : 2 annotation(s)" in p for p in ReviewPoints.generate(report))


def test_la_synthese_propre_ne_declenche_rien(tmp_path):
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.models import Job, JobState

    fs = JobFilesystem(str(tmp_path), "job-b")
    fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:04,000\nSPEAKER_00: ok.\n")
    fs.save_json("metadata/transcription_segments.json", [{"start": 0.0, "end": 4.0, "speaker": "A", "text": "ok."}])
    fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 4})
    fs.save_json("context/meeting_context.json", {"summary_llm": "Synthèse propre (budget 2026)."})
    report = QualityReporter({"storage": {"jobs_dir": str(tmp_path)}}).run_all_checks(
        Job(id="job-b", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value))
    assert not [c for c in report["checks"] if c["type"] == "synthesis_inline_annotations"]


@pytest.mark.parametrize("lang", LANGS)
def test_le_prompt_de_resume_interdit_les_annotations_en_ligne(lang):
    text = (ROOT / lang / "summary_prompt.txt").read_text(encoding="utf-8")
    # La règle vit dans le §8 (règles de contenu), juste après la puce « Synthèse ».
    assert any(k in text for k in ("Aucune annotation de doute", "No doubt annotations", "Keine Zweifelsvermerke",
                                   "Ninguna anotación de duda", "Nessuna annotazione di dubbio",
                                   "Nenhuma anotação de dúvida"))


@pytest.mark.parametrize("lang", LANGS)
def test_la_relecture_finale_retire_les_annotations(lang):
    text = (ROOT / lang / "final_review_prompt.txt").read_text(encoding="utf-8")
    assert "summary_harmonized.md" in text
    assert any(k in text for k in ("annotations de doute de transcription", "transcription-doubt annotations",
                                   "Transkriptions-Zweifelsvermerke", "anotaciones de duda de transcripción",
                                   "annotazioni di dubbio di trascrizione", "anotações de dúvida de transcrição"))

"""Options de rendu DOCX data-driven (``context/render_options.json``) — GPU-free.

Contrat : la LLM (ou l'utilisateur, via l'UI) choisit *quoi* rendre ; le renderer
garantit *comment*. Options v1 : ``theme`` (clé de ``_THEMES``, prime sur le
``meeting_type``) et ``sections`` (booléens : ``participants`` / ``transcript`` /
``quality``). Tout invalide est ignoré silencieusement — le rendu ne casse JAMAIS.
"""
import json
from pathlib import Path

import pytest
from test_docx_report import _seed_job

pytest.importorskip("docx")


def _render(tmp_path, options: dict | None) -> "object":
    from docx import Document

    from transcria.exports.docx_report import generate_docx_report

    job_id = "job-options"
    _seed_job(tmp_path, job_id)
    if options is not None:
        (tmp_path / job_id / "context" / "render_options.json").write_text(
            json.dumps(options), encoding="utf-8"
        )
    out = tmp_path / "rapport.docx"
    generate_docx_report(job_id, str(tmp_path), out)
    return Document(str(out))


def _all_text(doc) -> str:
    parts = [p.text for p in doc.paragraphs]
    parts += [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
    return "\n".join(parts)


class TestThemeOverride:
    def test_theme_overrides_meeting_type(self, tmp_path):
        # meeting_type="Autre" mais theme="CSE" → bandeau CSE appliqué.
        doc = _render(tmp_path, {"theme": "CSE"})
        assert "PROCÈS-VERBAL DU COMITÉ SOCIAL ET ÉCONOMIQUE" in _all_text(doc)

    def test_unknown_theme_ignored(self, tmp_path):
        ref = _all_text(_render(tmp_path, None))
        doc = _all_text(_render(tmp_path, {"theme": "zzz-inexistant"}))
        assert doc == ref  # rendu par défaut, aucune exception


def _section_headings(doc) -> list[str]:
    """Titres de section numérotés (« N.  LIBELLÉ ») — ignore le bandeau de couverture."""
    import re

    return [p.text for p in doc.paragraphs if re.match(r"^\d+\.\s{2}\S", p.text)]


class TestSectionToggles:
    def test_default_has_all_sections(self, tmp_path):
        heads = "\n".join(_section_headings(_render(tmp_path, None)))
        assert "TRANSCRIPTION" in heads
        assert "PARTICIPANTS & LOCUTEURS" in heads
        assert "POINTS À VÉRIFIER" in heads

    def test_transcript_off(self, tmp_path):
        heads = "\n".join(_section_headings(_render(tmp_path, {"sections": {"transcript": False}})))
        assert "TRANSCRIPTION" not in heads
        assert "PARTICIPANTS & LOCUTEURS" in heads  # les autres restent

    def test_participants_and_quality_off(self, tmp_path):
        heads = "\n".join(_section_headings(
            _render(tmp_path, {"sections": {"participants": False, "quality": False}})))
        assert "PARTICIPANTS & LOCUTEURS" not in heads
        assert "POINTS À VÉRIFIER" not in heads
        assert "TRANSCRIPTION" in heads

    def test_numbering_stays_sequential_when_section_skipped(self, tmp_path):
        # participants off → les numéros restent séquentiels (pas de trou 2. → 4.).
        doc = _render(tmp_path, {"sections": {"participants": False}})
        nums = [int(h.split(".")[0]) for h in _section_headings(doc)]
        assert nums == list(range(1, len(nums) + 1)), f"numérotation non séquentielle : {nums}"


class TestRobustness:
    def test_garbage_options_ignored(self, tmp_path):
        ref = _all_text(_render(tmp_path, None))
        doc = _all_text(_render(tmp_path, {"theme": 42, "sections": "junk", "autre": []}))
        assert doc == ref

    def test_corrupt_json_ignored(self, tmp_path):
        job_id = "job-options"
        _seed_job(tmp_path, job_id)
        (tmp_path / job_id / "context" / "render_options.json").write_text("{pas du json", encoding="utf-8")
        from transcria.exports.docx_report import generate_docx_report
        out = tmp_path / "rapport.docx"
        generate_docx_report(job_id, str(tmp_path), out)  # ne lève pas
        assert Path(out).is_file()

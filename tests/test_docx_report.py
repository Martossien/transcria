"""Tests pour le générateur de rapport DOCX."""
import re
from pathlib import Path

import pytest


JOB_ID   = "8ead05eb-c8f7-4c6e-9694-8c6d9c9dc230"
JOBS_DIR = str(Path(__file__).parent.parent / "jobs")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def generated_docx(tmp_path_factory):
    """Génère le rapport DOCX une seule fois pour tous les tests du module."""
    docx = pytest.importorskip("docx")  # skip si python-docx absent
    from transcria.exports.docx_report import generate_docx_report

    out = tmp_path_factory.mktemp("docx") / "rapport_test.docx"
    generate_docx_report(JOB_ID, JOBS_DIR, out)
    return out


@pytest.fixture(scope="module")
def doc(generated_docx):
    from docx import Document
    return Document(str(generated_docx))


# ── Tests fichier ──────────────────────────────────────────────────────────────

def test_fichier_cree(generated_docx):
    assert generated_docx.is_file()
    assert generated_docx.stat().st_size > 5000


# ── Tests contenu ─────────────────────────────────────────────────────────────

def _all_text(doc) -> str:
    return "\n".join(p.text for p in doc.paragraphs)


def _table_texts(doc) -> list[str]:
    texts = []
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.append(cell.text.strip())
    return texts


def test_titre_present(doc):
    text = _all_text(doc)
    assert "FROMAGERIE" in text.upper() or "COMTÉ" in text.upper()


def test_section_contexte(doc):
    text = _all_text(doc)
    assert "CONTEXTE" in text.upper()


def test_section_participants(doc):
    text = _all_text(doc)
    assert "PARTICIPANTS" in text.upper()


def test_section_transcription(doc):
    text = _all_text(doc)
    assert "TRANSCRIPTION" in text.upper()


def test_participants_dans_tableau(doc):
    texts = _table_texts(doc)
    joined = " ".join(texts)
    assert "Cliente" in joined
    assert "Vendeur" in joined


def test_temps_parole_calcule(doc):
    texts = _table_texts(doc)
    joined = " ".join(texts)
    # Doit contenir un pourcentage calculé
    assert re.search(r"\d+%", joined)


def test_transcription_contient_repliques(doc):
    texts = _table_texts(doc)
    joined = " ".join(texts)
    assert "émental" in joined.lower() or "comté" in joined.lower()


def test_timestamps_presents(doc):
    texts = _table_texts(doc)
    joined = " ".join(texts)
    assert re.search(r"\d{2}:\d{2}:\d{2}", joined)


def test_section_qualite_presente(doc):
    """Le job test a coverage 79% → la section doit apparaître."""
    text = _all_text(doc) + " ".join(_table_texts(doc))
    assert "VÉRIFIER" in text.upper() or "79" in text


def test_pas_de_confidentiel_par_defaut(doc):
    text = _all_text(doc) + " ".join(_table_texts(doc))
    assert "CONFIDENTIEL" not in text.upper()


def test_score_qualite_dans_document(doc):
    text = _all_text(doc) + " ".join(_table_texts(doc))
    assert "80" in text  # score qualité du job test


# ── Tests champs vides ─────────────────────────────────────────────────────────

def test_genere_sans_participants(tmp_path):
    pytest.importorskip("docx")
    from transcria.exports.docx_report import DocxReport, generate_docx_report

    out = tmp_path / "vide.docx"
    report = DocxReport({}, [], {}, {}, "")
    doc = report.build()
    doc.save(str(out))
    assert out.is_file()


def test_genere_avec_sensitivity_high(tmp_path):
    pytest.importorskip("docx")
    from docx import Document
    from transcria.exports.docx_report import DocxReport

    out = tmp_path / "confidentiel.docx"
    ctx = {"title": "Test confidentiel", "sensitivity": "high"}
    report = DocxReport(ctx, [], {}, {}, "")
    doc = report.build()
    doc.save(str(out))

    loaded = Document(str(out))
    texts = "\n".join(p.text for p in loaded.paragraphs)
    tables_text = " ".join(c.text for t in loaded.tables for r in t.rows for c in r.cells)
    assert "CONFIDENTIEL" in (texts + tables_text).upper()


def test_srt_parser_format_avec_locuteur():
    from transcria.exports.docx_report import _parse_srt

    srt = (
        "1\n00:00:01,012 --> 00:00:03,910\n"
        "SPEAKER_01(Vendeur / fromager): Podcast francefacil.com\n\n"
        "2\n00:00:05,416 --> 00:00:06,762\n"
        "SPEAKER_00(Cliente): Fais pas chaud ce matin.\n\n"
    )
    entries = _parse_srt(srt)
    assert len(entries) == 2
    assert entries[0]["speaker"] == "Vendeur / fromager"
    assert entries[0]["text"] == "Podcast francefacil.com"
    assert entries[0]["timestamp"] == "00:00:01"
    assert entries[1]["speaker"] == "Cliente"


def test_srt_parser_sans_locuteur():
    from transcria.exports.docx_report import _parse_srt

    srt = "1\n00:00:01,000 --> 00:00:03,000\nTexte sans locuteur.\n\n"
    entries = _parse_srt(srt)
    assert len(entries) == 1
    assert entries[0]["speaker"] == ""
    assert entries[0]["text"] == "Texte sans locuteur."


def test_fmt_date_valide():
    from transcria.exports.docx_report import _fmt_date

    assert _fmt_date("2026-05-29") == "29 mai 2026"
    assert _fmt_date("") == "—"
    assert _fmt_date("2026-01-15") == "15 janvier 2026"


def test_fmt_date_invalide():
    from transcria.exports.docx_report import _fmt_date

    assert _fmt_date("pas-une-date") == "pas-une-date"


def test_extract_synthese_avec_section():
    from transcria.exports.docx_report import _extract_synthese

    md = "## Informations\nblah\n## Synthèse\nVoici la synthèse.\n## Autre\nfin"
    result = _extract_synthese(md)
    assert "Voici la synthèse" in result
    assert "Autre" not in result


def test_merge_participants_calcule_pourcentages():
    from transcria.exports.docx_report import DocxReport

    participants = [{"id": "p1", "name": "Alice", "function": "", "service": "", "role": "", "is_animator": False}]
    speaker_stats = {"speakers": [{"speaker_id": "SPEAKER_00", "mapped_to": "p1", "speaking_time_seconds": 30.0, "turn_count": 10}]}
    report = DocxReport({}, participants, speaker_stats, {}, "")
    assert report.merged[0]["time_pct"] == 100
    assert report.merged[0]["turns"] == 10
    assert report.merged[0]["name"] == "Alice"

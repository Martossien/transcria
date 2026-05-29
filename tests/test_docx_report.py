"""Tests pour le générateur de rapport DOCX — données synthétiques, pas de job sur disque."""
import re
from pathlib import Path

import pytest


# ── Données synthétiques (reproduisent la structure réelle d'un job) ──────────

_CTX = {
    "title": "Scène de fromagerie — achat de comté",
    "meeting_type": "Autre",
    "date": "2026-05-29",
    "service": "Pédagogie",
    "language": "fr",
    "topic": "Dialogue en fromagerie",
    "objective": "Illustrer un échange commercial.",
    "notes": "Scène pédagogique.",
    "summary": "La cliente achète du comté et du beurre. Échange poli.",
    "sensitivity": "normal",
}

_PARTICIPANTS = [
    {"id": "p1", "name": "Cliente", "function": "", "service": "",
     "role": "pose des questions", "is_animator": False, "expected": True, "comment": ""},
    {"id": "p2", "name": "Vendeur / fromager", "function": "", "service": "",
     "role": "propose les produits", "is_animator": False, "expected": True, "comment": ""},
]

_SPEAKER_STATS = {
    "speakers": [
        {"speaker_id": "SPEAKER_00", "label": "SPEAKER_00", "mapped_to": "p1",
         "mapped_name": "Cliente", "speaking_time_seconds": 23.2,
         "turn_count": 15, "validation": "user_validated", "gender": "female"},
        {"speaker_id": "SPEAKER_01", "label": "SPEAKER_01", "mapped_to": "p2",
         "mapped_name": "Vendeur / fromager", "speaking_time_seconds": 25.7,
         "turn_count": 14, "validation": "user_validated", "gender": "male"},
    ]
}

_SRT = (
    "1\n00:00:01,012 --> 00:00:03,910\n"
    "SPEAKER_01(Vendeur / fromager): Podcast francefacil.com\n\n"
    "2\n00:00:05,416 --> 00:00:06,762\n"
    "SPEAKER_00(Cliente): Fais pas chaud ce matin.\n\n"
    "3\n00:00:11,592 --> 00:00:14,069\n"
    "SPEAKER_00(Cliente): Mettez-moi un peu d'émental s'il vous plaît.\n\n"
    "4\n00:00:19,827 --> 00:00:22,152\n"
    "SPEAKER_00(Cliente): Je prendrai bien un morceau de comté.\n\n"
)

_QUALITY = {
    "quality_score": 80,
    "total_checks": 16,
    "warnings": 4,
    "checks": [
        {"type": "low_coverage", "ratio": 0.79, "severity": "error"},
        {"type": "audio_problem_segments", "count": 1, "severity": "warning",
         "examples": [{"label": "silence", "start": 32.288, "end": 34.592,
                       "start_label": "00:32", "end_label": "00:35", "duration_s": 2.304}]},
    ],
    "review_points": ["Couverture faible : 79%", "Zone à réécouter : 00:32→00:35"],
}


def _seed_job(tmp_dir: Path, job_id: str) -> None:
    """Crée les fichiers d'un job synthétique dans tmp_dir."""
    import json
    base = tmp_dir / job_id
    for sub in ("context", "speakers", "metadata", "quality", "exports"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    (base / "context" / "meeting_context.json").write_text(json.dumps(_CTX), encoding="utf-8")
    (base / "context" / "participants.json").write_text(json.dumps(_PARTICIPANTS), encoding="utf-8")
    (base / "speakers" / "speaker_stats.json").write_text(json.dumps(_SPEAKER_STATS), encoding="utf-8")
    (base / "metadata" / "transcription_corrigee.srt").write_text(_SRT, encoding="utf-8")
    (base / "quality" / "quality_report.json").write_text(json.dumps(_QUALITY), encoding="utf-8")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def generated_docx(tmp_path_factory):
    """Génère le rapport DOCX une seule fois à partir de données synthétiques."""
    pytest.importorskip("docx")
    from transcria.exports.docx_report import generate_docx_report

    tmp = tmp_path_factory.mktemp("jobs_seed")
    job_id = "test-fromagerie-001"
    _seed_job(tmp, job_id)

    out = tmp_path_factory.mktemp("docx") / "rapport_test.docx"
    generate_docx_report(job_id, str(tmp), out)
    return out


@pytest.fixture(scope="module")
def doc(generated_docx):
    from docx import Document
    return Document(str(generated_docx))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_text(doc) -> str:
    return "\n".join(p.text for p in doc.paragraphs)


def _table_texts(doc) -> list[str]:
    return [cell.text.strip() for table in doc.tables
            for row in table.rows for cell in row.cells]


# ── Tests fichier ──────────────────────────────────────────────────────────────

def test_fichier_cree(generated_docx):
    assert generated_docx.is_file()
    assert generated_docx.stat().st_size > 5000


# ── Tests contenu ─────────────────────────────────────────────────────────────

def test_titre_present(doc):
    text = _all_text(doc)
    assert "FROMAGERIE" in text.upper() or "COMTÉ" in text.upper()


def test_section_contexte(doc):
    assert "CONTEXTE" in _all_text(doc).upper()


def test_section_participants(doc):
    assert "PARTICIPANTS" in _all_text(doc).upper()


def test_section_transcription(doc):
    assert "TRANSCRIPTION" in _all_text(doc).upper()


def test_participants_dans_tableau(doc):
    joined = " ".join(_table_texts(doc))
    assert "Cliente" in joined
    assert "Vendeur" in joined


def test_temps_parole_calcule(doc):
    joined = " ".join(_table_texts(doc))
    assert re.search(r"\d+%", joined)


def test_transcription_contient_repliques(doc):
    joined = " ".join(_table_texts(doc)).lower()
    assert "émental" in joined or "comté" in joined


def test_timestamps_presents(doc):
    joined = " ".join(_table_texts(doc))
    assert re.search(r"\d{2}:\d{2}:\d{2}", joined)


def test_section_qualite_presente(doc):
    """Coverage 79% → la section 'Points à vérifier' doit apparaître."""
    full = _all_text(doc) + " ".join(_table_texts(doc))
    assert "VÉRIFIER" in full.upper() or "79" in full


def test_pas_de_confidentiel_par_defaut(doc):
    full = _all_text(doc) + " ".join(_table_texts(doc))
    assert "CONFIDENTIEL" not in full.upper()


def test_score_qualite_dans_document(doc):
    full = _all_text(doc) + " ".join(_table_texts(doc))
    assert "80" in full


# ── Tests cas limites ─────────────────────────────────────────────────────────

def test_genere_sans_participants(tmp_path):
    pytest.importorskip("docx")
    from transcria.exports.docx_report import DocxReport

    out = tmp_path / "vide.docx"
    DocxReport({}, [], {}, {}, "").build().save(str(out))
    assert out.is_file()


def test_genere_avec_sensitivity_high(tmp_path):
    pytest.importorskip("docx")
    from docx import Document
    from transcria.exports.docx_report import DocxReport

    out = tmp_path / "confidentiel.docx"
    DocxReport({"title": "Test confidentiel", "sensitivity": "high"}, [], {}, {}, "").build().save(str(out))

    loaded = Document(str(out))
    full = ("\n".join(p.text for p in loaded.paragraphs)
            + " ".join(c.text for t in loaded.tables for r in t.rows for c in r.cells))
    assert "CONFIDENTIEL" in full.upper()


# ── Tests unitaires ───────────────────────────────────────────────────────────

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

    entries = _parse_srt("1\n00:00:01,000 --> 00:00:03,000\nTexte sans locuteur.\n\n")
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
    assert "Voici la synthèse" in _extract_synthese(md)
    assert "Autre" not in _extract_synthese(md)


def test_merge_participants_calcule_pourcentages():
    from transcria.exports.docx_report import DocxReport

    participants = [{"id": "p1", "name": "Alice", "function": "", "service": "",
                     "role": "", "is_animator": False}]
    speaker_stats = {"speakers": [{"speaker_id": "SPEAKER_00", "mapped_to": "p1",
                                    "speaking_time_seconds": 30.0, "turn_count": 10}]}
    report = DocxReport({}, participants, speaker_stats, {}, "")
    assert report.merged[0]["time_pct"] == 100
    assert report.merged[0]["turns"] == 10
    assert report.merged[0]["name"] == "Alice"

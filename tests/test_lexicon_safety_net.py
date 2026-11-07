"""Filet DÉTERMINISTE du lexique validé (2026-11-07).

Mesuré sur un job réel avant d'écrire ceci : la LLM de correction laissait 3 variantes sur 38
dans la transcription ; le titre, l'objectif et la synthèse (éditée à l'étape 4, donc AVANT la
validation du lexique à l'étape 6) gardaient la graphie fautive dans le DOCX. Un lexique validé
est une consigne : le code l'applique partout où un livrable est produit — transcription après
la LLM, rapport Word, summary.md du paquet, fiche de réunion, sources de « Discuter » — sans
jamais modifier les fichiers que l'utilisateur a écrits.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from transcria.context import lexicon as L

LEX = [
    {"term": "projet ALPHA", "category": "projet", "priority": "critique", "variants": ["projet Alfa"], "replace_by": ""},
    {"term": "Outil B", "category": "outil", "priority": "normale", "variants": ["Outil Bé", "outil bai"], "replace_by": ""},
    {"term": "Forme X / Forme Y", "category": "mot suspect", "priority": "normale", "variants": ["formix"], "replace_by": ""},
    {"term": "SIG", "category": "sigle", "priority": "critique", "variants": ["système intégré de gestion", "sigue"], "replace_by": ""},
    {"term": "Terme C", "category": "mot suspect", "priority": "normale", "variants": [], "replace_by": ""},
]

SRT = """1
00:00:01,000 --> 00:00:03,000
SPEAKER_00(Outil Bé): Le projet Alfa avance avec l'outil bai.

2
00:00:03,500 --> 00:00:05,000
SPEAKER_01: Le système intégré de gestion dit sigue, formix.
"""


# ── Règles de substitution ───────────────────────────────────────────────────

def test_entrees_autorisees_a_remplacer():
    allowed = L.substitution_entries(LEX, verbatim=True)
    terms = [e["term"] for e in allowed]
    assert "projet ALPHA" in terms and "Outil B" in terms and "SIG" in terms
    assert "Forme X / Forme Y" not in terms                      # forme ambiguë : jamais de substitution
    assert "Terme C" not in terms                                # sans variante : rien à remplacer
    sig = next(e for e in allowed if e["term"] == "SIG")
    assert sig["variants"] == ["sigue"]                          # verbatim : l'expansion prononcée du sigle reste
    free = next(e for e in L.substitution_entries(LEX) if e["term"] == "SIG")
    assert "système intégré de gestion" in free["variants"]     # texte rédigé : la forme validée s'applique


def test_apply_validated_forms_honore_les_formes_ambigues():
    assert L.apply_validated_forms("formix reste", LEX) == "formix reste"
    assert L.apply_validated_forms("Le projet Alfa et l'outil bai", LEX) == "Le projet ALPHA et l'Outil B"
    assert L.apply_validated_forms("le système intégré de gestion", LEX) == "le SIG"


def test_compte_des_variantes_restantes():
    assert L.count_remaining_variants("projet Alfa, outil bai, formix, sigue", LEX) == 3   # formix exclu


# ── SRT ──────────────────────────────────────────────────────────────────────

def test_harmonize_srt_texte_seulement():
    out, n = L.harmonize_srt(SRT, LEX)
    assert n == 3
    assert "projet ALPHA" in out and "Outil B." in out and "dit SIG," in out
    assert "SPEAKER_00(Outil Bé):" in out                        # préfixe locuteur intact (mapping)
    assert "00:00:01,000 --> 00:00:03,000" in out and out.startswith("1\n")
    assert "formix" in out and "système intégré de gestion" in out   # exclusions respectées
    assert L.harmonize_srt(SRT, []) == (SRT, 0) and L.harmonize_srt("", LEX) == ("", 0)
    again, n2 = L.harmonize_srt(out, LEX)
    assert again == out and n2 == 0                              # idempotent


# ── Contexte et données structurées ──────────────────────────────────────────

def test_harmonize_context_copie_et_champs_texte():
    ctx = {"title": "Point projet Alfa", "objective": "déployer l'outil bai", "summary": "Le projet Alfa…",
           "summary_harmonized": "projet Alfa", "summary_llm": "projet Alfa", "topic": "Outil Bé", "notes": "x",
           "date": "2026-11-07", "structured_data": {"decisions": ["Valider le projet Alfa"], "actions": [{"text": "outil bai"}]}}
    out = L.harmonize_context_fields(ctx, LEX)
    assert out["title"] == "Point projet ALPHA" and out["objective"] == "déployer l'Outil B"
    assert out["summary"] == "Le projet ALPHA…" and out["topic"] == "Outil B" and out["date"] == "2026-11-07"
    assert ctx["title"] == "Point projet Alfa"                   # l'original n'est pas touché
    sd = L.harmonize_structured(ctx["structured_data"], LEX)
    assert sd == {"decisions": ["Valider le projet ALPHA"], "actions": [{"text": "Outil B"}]}
    assert ctx["structured_data"]["decisions"] == ["Valider le projet Alfa"]
    assert L.harmonize_context_fields(None, LEX) == {} and L.harmonize_context_fields(ctx, []) == ctx


def test_load_session_lexicon_tolerant(tmp_path):
    assert L.load_session_lexicon(tmp_path) == []
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "session_lexicon.json").write_text("{pas du json", encoding="utf-8")
    assert L.load_session_lexicon(tmp_path) == []
    (tmp_path / "context" / "session_lexicon.json").write_text(json.dumps(LEX), encoding="utf-8")
    assert len(L.load_session_lexicon(tmp_path)) == 5


# ── Phase de correction : filet après la LLM ─────────────────────────────────

def test_filet_apres_la_llm_journalise_et_remplace(tmp_path, caplog):
    import logging

    from transcria.jobs.filesystem import JobFilesystem
    from transcria.workflow.phases.correction import apply_lexicon_safety_net

    fs = JobFilesystem(str(tmp_path), "job-filet")
    (fs.job_dir / "context").mkdir(parents=True, exist_ok=True)
    (fs.job_dir / "context" / "session_lexicon.json").write_text(json.dumps(LEX), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="transcria.workflow.phases.correction"):
        out = apply_lexicon_safety_net(fs, SRT, "correction")
    assert "projet ALPHA" in out and "projet Alfa" not in out
    assert any("filet lexique : 3 variante(s)" in r.getMessage() for r in caplog.records)
    assert apply_lexicon_safety_net(JobFilesystem(str(tmp_path), "sans-lexique"), SRT, "correction") == SRT


# ── Rapport Word ─────────────────────────────────────────────────────────────

def test_docx_porte_les_formes_validees_partout(tmp_path):
    pytest.importorskip("docx")
    sys.path.insert(0, str(Path(__file__).parent))
    from test_docx_report import _seed_job

    from transcria.exports.docx_report import generate_docx_report
    job_id = "job-filet-docx"
    _seed_job(tmp_path, job_id)
    base = tmp_path / job_id
    ctx = json.loads((base / "context" / "meeting_context.json").read_text(encoding="utf-8"))
    ctx["title"] = "Réunion projet Alfa"
    ctx["objective"] = "Déployer l'outil bai sur le projet Alfa"
    ctx["summary"] = "Synthèse éditée : le projet Alfa est prêt, l'Outil Bé aussi."
    ctx["structured_data"] = {"decisions": ["Lancer le projet Alfa"], "actions": ["Former à l'outil bai"]}
    (base / "context" / "meeting_context.json").write_text(json.dumps(ctx), encoding="utf-8")
    (base / "metadata" / "transcription_corrigee.srt").write_text(SRT, encoding="utf-8")
    (base / "context" / "session_lexicon.json").write_text(json.dumps(LEX), encoding="utf-8")
    out = generate_docx_report(job_id, str(tmp_path), tmp_path / "filet.docx")
    from docx import Document
    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs) + "\n" + "\n".join(
        c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "projet Alfa" not in text and "outil bai" not in text
    assert "l'Outil Bé aussi" not in text and "l'Outil B aussi" in text     # synthèse éditée harmonisée
    assert text.count("Outil Bé") == 1                           # seul le NOM du locuteur (préfixe SRT) subsiste
    assert "projet ALPHA" in text and "Outil B" in text
    assert "formix" in text                                      # forme ambiguë : laissée telle quelle
    # Les fichiers du job restent intacts : le filet est un filet de RENDU.
    stored = json.loads((base / "context" / "meeting_context.json").read_text(encoding="utf-8"))
    assert stored["title"] == "Réunion projet Alfa"
    assert "projet Alfa" in (base / "metadata" / "transcription_corrigee.srt").read_text(encoding="utf-8")


# ── Fiche de réunion ─────────────────────────────────────────────────────────

def test_fiche_de_reunion_harmonisee_et_empreinte_sensible_au_lexique():
    from transcria.job_cards import projection as P

    ctx = {"title": "Point projet Alfa", "meeting_type": "Réunion projet", "date": "2026-11-07",
           "summary": "Le projet Alfa avance.", "structured_data": {"decisions": ["Valider l'outil bai"], "actions": []}}
    kw = dict(created_at=datetime(2026, 11, 7, tzinfo=UTC), confidential_types=frozenset(),
              known_types=frozenset({"Réunion projet"}), summary_headings=["## Synthèse"])
    plain = P.project_card(ctx, [], **kw)
    with_lex = P.project_card(ctx, [], lexicon=LEX, **kw)
    assert "projet Alfa" in plain.title and with_lex.title == "Point projet ALPHA"
    assert with_lex.abstract.startswith("Le projet ALPHA") and with_lex.decisions == ["Valider l'Outil B"]
    raw = json.dumps(ctx).encode()
    fp_a = P.source_fingerprint(raw, b"[]", created_at=kw["created_at"], confidential_types=frozenset(),
                                known_types=kw["known_types"])
    fp_b = P.source_fingerprint(raw, b"[]", created_at=kw["created_at"], confidential_types=frozenset(),
                                known_types=kw["known_types"], lexicon_raw=json.dumps(LEX).encode())
    assert fp_a != fp_b                                          # un lexique qui change ⇒ fiche recalculée


# ── « Discuter avec mes réunions » ───────────────────────────────────────────

def test_sources_discuter_harmonisees(tmp_path):
    from transcria.ask_meetings import sources

    job_dir = tmp_path / "jobs" / "job-ask"
    (job_dir / "context").mkdir(parents=True)
    (job_dir / "metadata").mkdir()
    (job_dir / "context" / "meeting_context.json").write_text(
        json.dumps({"summary": "Le projet Alfa est validé.", "language": "fr"}), encoding="utf-8")
    (job_dir / "context" / "session_lexicon.json").write_text(json.dumps(LEX), encoding="utf-8")
    (job_dir / "metadata" / "transcription_corrigee.srt").write_text(SRT, encoding="utf-8")
    cfg = {"storage": {"jobs_dir": str(tmp_path / "jobs"), "shared_backend": "fs"}}
    assert sources.effective_summary(cfg, "job-ask") == "Le projet ALPHA est validé."
    assert "projet ALPHA" in sources.effective_srt(cfg, "job-ask") and "projet Alfa" not in sources.effective_srt(cfg, "job-ask")

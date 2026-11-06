"""Rendu des réunions pour la LLM et garde des citations — PUR (docs/DISCUTER_REUNIONS.md § 2.6)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from transcria.ask_meetings.prompting import (
    NumberedMeeting,
    build_messages,
    card_doc,
    render_card,
    render_cards_block,
    render_summaries_block,
    render_transcript_block,
    verify_citations,
)
from transcria.search.preselect import CardDoc


def _fake_card(**kw):
    base = dict(title="Point projet", meeting_type="Réunion projet", meeting_date=date(2026, 5, 12),
                date_is_import_fallback=False, participants=[{"name": "Personne A", "function": "Fonction A"}],
                topic="Avancement", objective="Décider", agenda="1. Budget", abstract="Le budget est validé.",
                decisions=["Budget validé"], actions=["Personne A : devis (vendredi)"], next_date="juin")
    base.update(kw)
    return SimpleNamespace(**base)


def test_card_doc_porte_la_taille_de_son_rendu_et_les_participants():
    doc = card_doc("job-1", "titre du job", _fake_card(), locale="fr")
    assert doc.participants == ("Personne A Fonction A",)
    assert doc.rendered_chars == len(render_card(1, doc, date_is_import_fallback=False))
    assert doc.decisions == ("Budget validé",)


def test_render_card_contenu_et_numero():
    doc = card_doc("job-1", "titre du job", _fake_card(), locale="fr")
    text = render_card(7, doc, date_is_import_fallback=True, locale="fr")
    assert text.startswith("[7] Point projet")
    assert "2026-05-12 (date d'import)" in text
    assert "Décisions:\n- Budget validé" in text and "Actions:\n- Personne A : devis (vendredi)" in text
    assert "Prochaine échéance: juin" in text


def test_render_card_titre_du_job_en_repli_et_langue():
    doc = card_doc("job-1", "Nom du fichier", _fake_card(title=""), locale="en")
    text = render_card(1, doc, date_is_import_fallback=False, locale="en")
    assert text.startswith("[1] Nom du fichier") and "Decisions:" in text
    doc = card_doc("job-1", "", _fake_card(title=""), locale="de")
    assert "Besprechung ohne Titel" in render_card(1, doc, date_is_import_fallback=False, locale="de")


def test_blocs_niveau_1_2_3():
    doc = card_doc("job-1", "t", _fake_card(), locale="fr")
    m = NumberedMeeting(index=1, job_id="job-1", doc=doc, date_is_import_fallback=False)
    assert render_cards_block([m]).startswith("## Réunions consultables")
    level2 = render_summaries_block([m], {"job-1": "Synthèse complète."})
    assert "### Synthèse [1]\nSynthèse complète." in level2
    assert "### Synthèse [1]\n(vide)" in render_summaries_block([m], {})
    level3 = render_transcript_block(m, "1\n00:00 --> 00:01\nSPEAKER_00: bonjour", shown_pct=42)
    assert "### Transcription [1]\n(transcription tronquée : 42 % montrés)" in level3
    assert "(transcription tronquée" not in render_transcript_block(m, "x", shown_pct=None)


def test_build_messages_rejoue_l_historique_utile_seulement():
    msgs = build_messages(system_prompt="SYS", context_block="CTX", history=[
        {"role": "user", "text": "q1"}, {"role": "assistant", "text": "r1"},
        {"role": "system", "text": "ignoré"}, {"role": "user", "text": "  "},
    ], question="q2")
    assert msgs[0] == {"role": "system", "content": "SYS\n\nCTX\n"}
    assert [m["content"] for m in msgs[1:]] == ["q1", "r1", "q2"]


@pytest.mark.parametrize(("answer", "allowed", "expected_text", "expected_cited"), [
    ("Validé [1] et [2].", 3, "Validé [1] et [2].", [1, 2]),
    ("Validé [1, 2] puis [2;3].", 3, "Validé [1, 2] puis [2, 3].", [1, 2, 3]),
    ("Hors liste [7] et [0] ; vrai [3].", 3, "Hors liste et ; vrai [3].", [3]),
    ("Validé [9]. Hors liste : [8].", 2, "Validé. Hors liste.", []),
    ("Est-ce validé ? Oui [1] ! Voir : [9] ?", 2, "Est-ce validé ? Oui [1] ! Voir ?", [1]),   # espaces françaises gardées
    ("Mixte [1, 9].", 3, "Mixte [1].", [1]),
    ("[ 2 ] avec espaces", 3, "[2] avec espaces", [2]),
    ("Aucune citation.", 3, "Aucune citation.", []),
    ("Rien [1]", 0, "Rien", []),
    ("", 3, "", []),
    ("Pas une citation : [abc] ni [1234].", 3, "Pas une citation : [abc] ni [1234].", []),
])
def test_garde_des_citations(answer, allowed, expected_text, expected_cited):
    assert verify_citations(answer, allowed) == (expected_text, expected_cited)


def test_garde_des_citations_ne_laisse_jamais_passer_un_numero_hors_liste():
    text, cited = verify_citations("x [1][2][3][4][5][6][7][8][9][10][11][12]", 2)
    assert cited == [1, 2] and "[3]" not in text and "[12]" not in text


def test_card_doc_tolere_une_fiche_vide():
    empty = SimpleNamespace(title="", meeting_type="", meeting_date=None, date_is_import_fallback=True,
                            participants=[], topic="", objective="", agenda="", abstract="", decisions=[],
                            actions=[], next_date="")
    doc = card_doc("j", "", empty)
    assert isinstance(doc, CardDoc) and doc.rendered_chars > 0
    assert render_card(1, doc, date_is_import_fallback=True).startswith("[1] Réunion sans titre")

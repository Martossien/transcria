"""Présélection PURE des réunions (docs/DISCUTER_REUNIONS.md § 2.4) — aucune base, aucune LLM."""
from __future__ import annotations

from datetime import date

from transcria.search.preselect import (
    CardDoc,
    DateHint,
    date_hints,
    preselect,
    question_tokens,
    rank,
    score_doc,
)
from transcria.search.text import fold, same_word, tokens


def _doc(job_id: str, **kw) -> CardDoc:
    kw.setdefault("rendered_chars", 800)
    return CardDoc(job_id=job_id, **kw)


# ── Texte ────────────────────────────────────────────────────────────────────

def test_fold_et_tokens_sans_accents_ni_mots_outils():
    assert fold("  Décision   PRISE ") == "decision prise"
    assert tokens("Qu'a-t-on décidé sur le budget de la formation ?") == ["decide", "budget", "formation"]
    assert tokens("ab é 12") == []                          # jetons < 3 caractères ignorés
    assert tokens("会議 の 要約 budget") == ["budget"]
    assert tokens("") == [] and tokens(None) == []


def test_same_word_racine_commune():
    assert same_word("formation", "formations")
    assert same_word("budgets", "budget")
    assert not same_word("mars", "marseille")             # trop court pour un préfixe
    assert not same_word("budget", "budgie")
    assert same_word("plan", "plan")


# ── Dates de la question ─────────────────────────────────────────────────────

def test_dates_de_la_question():
    assert date_hints("la réunion du 12/05/2026") == [DateHint(2026, 5, 12)]
    assert date_hints("le 2026-05-12 à 14:00") == [DateHint(2026, 5, 12)]
    assert date_hints("qu'a-t-on décidé en octobre 2026 ?") == [DateHint(2026, 10, None)]
    assert date_hints("le 1er février 2027 et en march 2026") == [DateHint(2027, 2, 1), DateHint(2026, 3, None)]
    assert date_hints("réunion du 12 mai 2026") == [DateHint(2026, 5, 12)]        # le mois seul n'est pas redoublé
    assert date_hints("31/02/2026 et 2026-13-01") == []
    assert date_hints("") == []


# ── Score ────────────────────────────────────────────────────────────────────

def test_titre_et_sujet_pesent_plus_que_l_abrege():
    in_title = _doc("t", title="Nouveau prestataire de nettoyage")
    in_abstract = _doc("a", abstract="On a évoqué le nouveau prestataire en fin de séance.")
    ranked = rank([in_abstract, in_title], "nouveau prestataire")
    assert [r.doc.job_id for r in ranked] == ["t", "a"]
    assert ranked[0].score > ranked[1].score > 0
    assert ranked[0].matched == ["nouveau", "prestataire"]


def test_participant_cite_dans_la_question():
    with_person = _doc("p", participants=("Personne Alpha Fonction A",))
    other = _doc("o", participants=("Personne Beta",))
    ranked = rank([other, with_person], "la réunion avec Alpha")
    assert ranked[0].doc.job_id == "p" and ranked[1].score == 0


def test_bonus_de_date_jour_et_mois():
    q = "qu'a-t-on décidé le 12/05/2026 ?"
    same_day = score_doc(_doc("d", meeting_date=date(2026, 5, 12)), question_tokens(q), date_hints(q))
    same_month = score_doc(_doc("m", meeting_date=date(2026, 5, 3)), question_tokens(q), date_hints("en mai 2026"))
    other = score_doc(_doc("o", meeting_date=date(2026, 6, 12)), question_tokens(q), date_hints(q))
    none = score_doc(_doc("n", meeting_date=None), question_tokens(q), date_hints(q))
    assert same_day.score > same_month.score > other.score == none.score == 0


def test_a_score_egal_la_plus_recente_d_abord():
    old = _doc("old", title="Point budget", meeting_date=date(2025, 1, 1))
    new = _doc("new", title="Point budget", meeting_date=date(2026, 1, 1))
    undated = _doc("none", title="Point budget", meeting_date=None)
    assert [r.doc.job_id for r in rank([undated, old, new], "budget")] == ["new", "old", "none"]


def test_un_jeton_compte_une_fois_par_champ():
    repeated = _doc("r", abstract="budget budget budget budget")
    once = _doc("o", abstract="budget")
    ranked = rank([repeated, once], "budget")
    assert ranked[0].score == ranked[1].score


# ── Plafonds ─────────────────────────────────────────────────────────────────

def test_preselect_respecte_les_deux_plafonds():
    docs = [_doc(f"j{i}", title="budget", rendered_chars=4000, meeting_date=date(2026, 1, i + 1)) for i in range(10)]
    by_count = preselect(docs, "budget", max_cards=3, max_tokens=10_000_000)
    assert len(by_count) == 3
    by_tokens = preselect(docs, "budget", max_cards=100, max_tokens=2500)      # 1000 tokens par fiche
    assert len(by_tokens) == 2


def test_fiche_trop_grosse_sautee_pas_la_suite():
    big = _doc("big", title="budget", rendered_chars=400_000)
    small = _doc("small", title="budget", rendered_chars=400)
    kept = preselect([big, small], "budget", max_cards=10, max_tokens=1000)
    assert [r.doc.job_id for r in kept] == ["small"]


def test_sans_mot_discriminant_les_plus_recentes_sont_envoyees():
    docs = [_doc(f"j{i}", title=f"Réunion {i}", meeting_date=date(2026, 1, i + 1)) for i in range(5)]
    kept = preselect(docs, "qu'a-t-on décidé récemment ?", max_cards=2, max_tokens=100_000)
    assert [r.doc.job_id for r in kept] == ["j4", "j3"]
    assert all(r.score == 0 for r in kept)


def test_entrees_aux_limites():
    docs = [_doc("x", title="Sujet")]
    assert preselect(docs, "", max_cards=5, max_tokens=1000)[0].doc.job_id == "x"
    assert preselect(docs, "é", max_cards=5, max_tokens=1000)
    assert preselect(docs, "sujet " * 5000, max_cards=5, max_tokens=1000)[0].score > 0
    assert preselect([], "sujet", max_cards=5, max_tokens=1000) == []

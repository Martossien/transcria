"""Projection PURE contexte → fiche de réunion (docs/DISCUTER_REUNIONS.md § 2.3, lot A).

Aucune base, aucun disque. Les noms sont des substituts neutres.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from transcria.job_cards import projection as P

CONFIDENTIAL = frozenset({"Type confidentiel A"})
KNOWN = frozenset({"Type confidentiel A", "Réunion interne", "Réunion projet"})
HEADINGS = ["## Synthèse", "## Summary"]
CREATED = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)


def _card(ctx, participants=None, created_at=CREATED):
    return P.project_card(ctx, participants, created_at=created_at, confidential_types=CONFIDENTIAL,
                          known_types=KNOWN, summary_headings=HEADINGS)


# ── Date ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-05-12", date(2026, 5, 12)),
    ("2026-05-12T14:00:00", date(2026, 5, 12)),
    ("2026-5-2", date(2026, 5, 2)),
    ("12/05/2026", date(2026, 5, 12)),
    ("12.05.2026", date(2026, 5, 12)),
    ("12-05-26", date(2026, 5, 12)),
    ("05/23/2026", date(2026, 5, 23)),              # seul l'ordre mois/jour est possible
    ("12 mai 2026", date(2026, 5, 12)),
    ("Lundi 1er février 2027 à 14:00", date(2027, 2, 1)),
    ("3 DÉCEMBRE 2025", date(2025, 12, 3)),
    ("7 sept. 2026", date(2026, 9, 7)),
    ("réunion du 9 août 2026", date(2026, 8, 9)),
    ("١٢/٠٥/٢٠٢٦", date(2026, 5, 12)),                # chiffres arabes-indiens : même date
])
def test_date_formats_reconnus(raw, expected):
    assert P.parse_meeting_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, 0, "demain", "semaine 12", "31/02/2026", "2026-13-01", "12/05/1850",
    "5 jui 2026",                                   # « jui » : juin ou juillet, non tranché
    "14:00", "x" * 5000, ["2026-05-12"], {"date": "2026-05-12"}, 20260512,
])
def test_date_non_analysable_rend_none(raw):
    assert P.parse_meeting_date(raw) is None


def test_date_absente_retombe_sur_la_creation_et_le_dit():
    card = _card({"date": "bientôt"})
    assert card.meeting_date == date(2026, 3, 4)
    assert card.date_is_import_fallback is True
    card = _card({"date": "2026-01-15"})
    assert (card.meeting_date, card.date_is_import_fallback) == (date(2026, 1, 15), False)
    card = _card({}, created_at=None)
    assert (card.meeting_date, card.date_is_import_fallback) == (None, True)


# ── Confidentialité ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(("ctx", "expected"), [
    ({}, False),
    ({"meeting_type": "Réunion interne", "sensitivity": "normal"}, False),
    ({"meeting_type": "Réunion interne", "sensitivity": ""}, False),
    ({"meeting_type": "Réunion interne", "sensitivity": "high"}, True),
    ({"sensitivity": "Élevée"}, True),
    ({"meeting_type": "Type confidentiel A"}, True),
    ({"meeting_type": "  type CONFIDENTIEL a "}, True),
    ({"meeting_type": "Type sur mesure", "custom_type": {"behavior": {"confidential": True}}}, True),
    ({"meeting_type": "Type sur mesure", "custom_type": {"behavior": {"confidential": False}}}, False),
    ({"meeting_type": "Type sur mesure", "custom_type": "pas un dict"}, False),
    # Étape 4 non enregistrée (pas de clé meeting_type) : la suggestion décide, dans le doute confidentiel.
    ({"type_suggere": "Type confidentiel A"}, True),
    ({"type_suggere": "Réunion projet"}, False),
    ({"type_suggere": "Réunion projet (probable)"}, True),
    ({"type_suggere": "Type inconnu du catalogue"}, True),
    ({"type_suggere": ""}, False),
    # Étape 4 enregistrée : l'humain a tranché, la suggestion ne compte plus.
    ({"meeting_type": "Réunion interne", "type_suggere": "Type confidentiel A"}, False),
    ({"meeting_type": "Réunion interne", "type_suggere": "Type inconnu du catalogue"}, False),
])
def test_regle_confidentiel(ctx, expected):
    assert P.is_confidential(ctx, confidential_types=CONFIDENTIAL, known_types=KNOWN) is expected


def test_type_affiche_choisi_sinon_suggere():
    assert _card({"meeting_type": "Réunion projet", "type_suggere": "Réunion interne"}).meeting_type == "Réunion projet"
    assert _card({"type_suggere": "Réunion interne"}).meeting_type == "Réunion interne"
    assert _card({"meeting_type": None}).meeting_type == ""
    assert _card({}).meeting_type == ""


# ── Synthèse effective et abrégé ─────────────────────────────────────────────

def test_synthese_effective_priorite_editee_harmonisee_brute():
    raw = "**Titre suggéré**\nTitre\n\n## Synthèse\nTexte de la machine.\n\n## Participants probables\n- SPEAKER_00"
    # La synthèse harmonisée est un markdown COMPLET, comme le brut : seule sa section synthèse compte.
    harmonized = "**Titre suggéré**\nTitre\n\n## Synthèse\nTexte harmonisé.\n\n## Termes douteux à valider\n- terme"
    assert _card({"summary_llm": raw}).abstract == "Texte de la machine."
    assert _card({"summary_llm": raw, "summary_harmonized": harmonized}).abstract == "Texte harmonisé."
    assert _card({"summary_llm": raw, "summary_harmonized": harmonized,
                  "summary": "Texte édité."}).abstract == "Texte édité."
    assert _card({"summary_harmonized": "Harmonisée sans titre de section."}).abstract == "Harmonisée sans titre de section."
    assert _card({}).abstract == ""


def test_synthese_dans_une_autre_langue_que_celle_du_job():
    assert _card({"summary_llm": "## Summary\nMachine text.\n\n## Probable participants\n"}).abstract == "Machine text."


def test_abrege_borne_coupe_en_fin_de_phrase():
    text = "Première phrase assez longue pour compter. " * 40
    abstract = P.build_abstract(text)
    assert len(abstract) <= P.ABSTRACT_MAX_CHARS
    assert abstract.endswith(".…")


def test_abrege_sans_fin_de_phrase_coupe_au_mot_ou_net():
    words = P.build_abstract("mot " * 600)
    assert len(words) <= P.ABSTRACT_MAX_CHARS and words.endswith("mot…")
    block = P.build_abstract("x" * 5000)
    assert len(block) == P.ABSTRACT_MAX_CHARS and block.endswith("…")


def test_abrege_court_inchange_blancs_resserres():
    assert P.build_abstract("  Un   texte\n\n\n sur deux lignes.  ") == "Un texte\nsur deux lignes."
    assert P.build_abstract("") == ""
    assert P.build_abstract("é") == "é"
    assert P.build_abstract("会議の要約 🎯 — ça tient") == "会議の要約 🎯 — ça tient"


# ── Listes ───────────────────────────────────────────────────────────────────

def test_sujet_objectif_ordre_du_jour_valides_sinon_suggeres():
    suggested = {"sujet_suggere": "Sujet machine", "objectif_suggere": "Objectif machine",
                 "notes_suggeres": "1. Point A\n2. Point B"}
    card = _card(suggested)
    assert (card.topic, card.objective, card.agenda) == ("Sujet machine", "Objectif machine", "1. Point A 2. Point B")
    # Étape 4 enregistrée : ce que l'humain a écrit fait foi, y compris un champ qu'il a VIDÉ.
    card = _card({**suggested, "meeting_type": "Réunion interne", "topic": "Sujet humain", "objective": "",
                  "notes": None})
    assert (card.topic, card.objective, card.agenda) == ("Sujet humain", "", "")


def test_champs_courts_bornes_au_mot():
    card = _card({"sujet_suggere": "mot " * 500, "objectif_suggere": "o" * 5000, "notes_suggeres": ["liste"]})
    assert len(card.topic) <= P.TOPIC_MAX_CHARS and card.topic.endswith("mot…")
    assert len(card.objective) == P.OBJECTIVE_MAX_CHARS and card.objective.endswith("…")
    assert card.agenda == ""
    assert _card({"mots_cles": "jamais stocké par le pipeline, donc jamais lu"}).topic == ""


def test_decisions_actions_bornees_et_nettoyees():
    card = _card({"structured_data": {
        "decisions": ["  Décision A  ", "", None, {"objet": "ignoré"}, "Décision\nsur deux lignes"],
        "actions": ["Personne A : action (délai)"] * 100,
        "prochaine_date": "  mardi prochain  ",
    }})
    assert card.decisions == ["Décision A", "Décision sur deux lignes"]
    assert len(card.actions) == P.MAX_LIST_ITEMS
    assert card.next_date == "mardi prochain"
    long_item = _card({"structured_data": {"actions": ["z" * 3000]}}).actions[0]
    assert len(long_item) == P.MAX_ITEM_CHARS and long_item.endswith("…")
    assert _card({"structured_data": {"actions": "Une seule action"}}).actions == ["Une seule action"]


def test_participants_nommes_seulement_sans_doublon():
    card = _card({}, [
        {"name": "Personne A", "function": "Fonction A", "role": "long rôle ignoré"},
        {"name": "SPEAKER_01"}, {"name": ""}, {"name": "PISTE_ab_S1"},
        {"name": "personne a", "function": "doublon"},
        "pas un dict", None,
        {"name": "Personne   B"},
    ])
    assert card.participants == [{"name": "Personne A", "function": "Fonction A"},
                                 {"name": "Personne B", "function": ""}]
    assert len(_card({}, [{"name": f"Personne {i}"} for i in range(500)]).participants) == P.MAX_PARTICIPANTS


def test_titre_du_contexte_sinon_suggere_borne():
    assert _card({"title": "  Titre   saisi ", "title_suggere": "Suggéré"}).title == "Titre saisi"
    assert _card({"title": "", "title_suggere": "Suggéré"}).title == "Suggéré"
    assert len(_card({"title": "t" * 4000}).title) == 255


# ── Formes inattendues : jamais d'exception ──────────────────────────────────

@pytest.mark.parametrize("ctx", [
    None, [], "texte", 7,
    {"title": None, "date": None, "sensitivity": None, "language": None, "structured_data": None},
    {"title": ["liste"], "date": {"a": 1}, "sensitivity": 3, "language": 12, "topic": {"a": 1},
     "objective": 4, "notes": [], "sujet_suggere": 1.5,
     "structured_data": "chaîne", "summary": ["liste"], "summary_llm": 5, "custom_type": [],
     "type_suggere": ["liste"], "meeting_type": {"nom": "x"}},
    {"structured_data": {"decisions": {"a": 1}, "actions": 9, "prochaine_date": ["x"]}},
])
@pytest.mark.parametrize("participants", [None, {}, "texte", [1, 2], [{"name": None, "function": None}]])
def test_formes_inattendues_ne_levent_jamais(ctx, participants):
    card = _card(ctx, participants)
    assert isinstance(card.confidential, bool)
    assert isinstance(card.abstract, str) and len(card.abstract) <= P.ABSTRACT_MAX_CHARS
    assert len(card.sensitivity) <= 20 and len(card.language) <= 8
    assert all(isinstance(v, str) for v in (card.title, card.meeting_type, card.topic, card.objective,
                                             card.agenda, card.next_date))


# ── Empreinte ────────────────────────────────────────────────────────────────

def _fp(meeting=b"{}", participants=b"[]", created_at=CREATED, confidential=CONFIDENTIAL, known=KNOWN):
    return P.source_fingerprint(meeting, participants, created_at=created_at,
                                confidential_types=confidential, known_types=known)


def test_empreinte_stable_et_sensible_a_chaque_dependance():
    base = _fp()
    assert base == _fp() and len(base) == 64
    assert _fp(meeting=b'{"a": 1}') != base
    assert _fp(participants=b'[{"name": "x"}]') != base
    assert _fp(created_at=datetime(2026, 3, 5, tzinfo=UTC)) != base
    assert _fp(confidential=frozenset({"Autre type"})) != base
    assert _fp(known=KNOWN | {"Nouveau type"}) != base
    # Fichier absent ≠ fichier vide ; et les deux fichiers ne sont pas interchangeables.
    assert _fp(meeting=None) != _fp(meeting=b"")
    assert _fp(meeting=b"ab", participants=b"c") != _fp(meeting=b"a", participants=b"bc")


def test_version_de_projection_dans_l_empreinte(monkeypatch):
    base = _fp()
    monkeypatch.setattr(P, "CARD_VERSION", P.CARD_VERSION + 1)
    assert _fp() != base

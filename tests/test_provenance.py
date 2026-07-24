"""Couture 1 (temps réel) — provenance des segments."""
from transcria.stt.provenance import (
    CANONICAL,
    FINAL_LIVE,
    PROVENANCES,
    stamp_provenance,
)


def test_defaut_canonical():
    segs = [{"start": 0.0, "end": 1.0, "text": "a"}, {"start": 1.0, "end": 2.0, "text": "b"}]
    out = stamp_provenance(segs)
    assert out is segs  # mutation en place + retour pour chaînage
    assert all(s["provenance"] == CANONICAL for s in out)


def test_idempotent_ne_surcharge_pas_une_provenance_existante():
    # Le live aura posé sa valeur ; le batch ne doit pas l'écraser.
    segs = [{"text": "live", "provenance": FINAL_LIVE}, {"text": "neuf"}]
    stamp_provenance(segs)
    assert segs[0]["provenance"] == FINAL_LIVE  # préservé
    assert segs[1]["provenance"] == CANONICAL   # posé par défaut


def test_valeur_explicite():
    segs = [{"text": "x"}]
    stamp_provenance(segs, FINAL_LIVE)
    assert segs[0]["provenance"] == FINAL_LIVE


def test_sur_liste_vide_et_non_dict():
    assert stamp_provenance([]) == []
    # robuste si un élément non-dict traîne (ne lève pas)
    segs = [{"text": "ok"}, None]  # type: ignore[list-item]
    stamp_provenance(segs)
    assert segs[0]["provenance"] == CANONICAL


def test_les_4_etats_definis():
    assert set(PROVENANCES) == {"partial", "provisional", "final_live", "canonical"}
    assert CANONICAL == "canonical"

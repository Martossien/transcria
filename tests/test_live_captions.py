"""Suivi en direct — module PUR `live_captions` (vague 5, lot C, D5.5).

Ce que ces tests verrouillent : le fichier est PLAFONNÉ par troncature de tête ANNONCÉE
(marqueur dans le flux, jamais silencieuse), la numérotation `n` reste MONOTONE à travers
la troncature (le poll en delta ne rejoue ni ne perd un tour), et une entrée douteuse est
écartée sans jamais lever (le direct est provisoire — jamais une raison d'échouer).
"""
from __future__ import annotations

from transcria.ingestion.live_captions import (
    append_captions,
    read_captions,
    sanitize_caption,
)


def _caption(n, text="Bonjour", speaker="Alice"):
    return {"start": float(n), "end": float(n) + 1.0, "speaker": speaker, "text": text}


class TestSanitize:
    def test_entree_valide_normalisee(self):
        out = sanitize_caption({"start": "1.5", "end": 2, "speaker": " Alice ", "text": " Bonjour "})
        assert out == {"start": 1.5, "end": 2.0, "speaker": "Alice", "text": "Bonjour"}

    def test_entrees_douteuses_ecartees_sans_lever(self):
        for raw in (None, "texte", {}, {"text": "  "}, {"text": "x", "start": "abc"}):
            assert sanitize_caption(raw) is None

    def test_bornes_de_taille(self):
        out = sanitize_caption({"start": -3, "end": 1, "speaker": "s" * 300, "text": "t" * 900})
        assert out["start"] == 0.0 and len(out["speaker"]) == 120 and len(out["text"]) == 500


class TestAppendAndRead:
    def test_ajout_et_lecture_en_delta(self, tmp_path):
        path = tmp_path / "captions.jsonl"
        assert append_captions(path, [_caption(1), _caption(2)]) == 2
        first, cursor, truncated = read_captions(path, 0)
        assert [c["n"] for c in first] == [1, 2] and cursor == 2 and truncated == 0
        append_captions(path, [_caption(3)])
        fresh, cursor, _ = read_captions(path, cursor)
        assert [c["n"] for c in fresh] == [3] and cursor == 3
        assert read_captions(path, cursor)[0] == []           # rien de neuf : delta vide

    def test_plafond_troncature_de_tete_annoncee(self, tmp_path):
        path = tmp_path / "captions.jsonl"
        append_captions(path, [_caption(i) for i in range(8)], max_lines=5)
        records, cursor, truncated = read_captions(path, 0)
        assert truncated == 3                                  # annoncé, jamais silencieux
        assert [c["n"] for c in records] == [4, 5, 6, 7, 8]    # la TÊTE est partie
        assert cursor == 8
        # La numérotation reste monotone après une nouvelle vague tronquée.
        append_captions(path, [_caption(9), _caption(10)], max_lines=5)
        records, cursor, truncated = read_captions(path, 8)
        assert [c["n"] for c in records] == [9, 10] and truncated == 5

    def test_fichier_absent_ou_corrompu_jamais_une_erreur(self, tmp_path):
        assert read_captions(tmp_path / "absent.jsonl", 0) == ([], 0, 0)
        broken = tmp_path / "broken.jsonl"
        broken.write_text("pas du json\n{\"n\": 1, \"text\": \"ok\"}\n[1,2]\n")
        records, cursor, _ = read_captions(broken, 0)
        assert [c["text"] for c in records] == ["ok"] and cursor == 1
        append_captions(broken, [_caption(2)])                 # on repart proprement
        assert read_captions(broken, 1)[0][0]["n"] == 2

"""Rendu SRT sans objet transcriber (façade en mode délégation au nœud de calcul)."""
from __future__ import annotations

from transcria.web.facade_format import segments_to_srt


def test_rendu_srt_conforme():
    srt = segments_to_srt([
        {"start": 0.0, "end": 1.5, "text": "Bonjour à tous"},
        {"start": 1.5, "end": 3.25, "text": "Merci"},
    ])
    lignes = srt.split("\n")
    assert lignes[0] == "1"
    assert lignes[1] == "00:00:00,000 --> 00:00:01,500"
    assert lignes[2] == "Bonjour à tous"
    assert "00:00:01,500 --> 00:00:03,250" in srt


def test_segments_vides_ignores_et_numerotation_continue():
    srt = segments_to_srt([
        {"start": 0.0, "end": 1.0, "text": "un"},
        {"start": 1.0, "end": 2.0, "text": "   "},      # vide → sauté
        {"start": 2.0, "end": 3.0, "text": "deux"},
    ])
    assert "\n1\n" not in srt.replace(srt.split("\n")[0], "", 1)  # pas de doublon d'index
    assert srt.count("-->") == 2 and "2\n00:00:02,000" in srt


def test_locuteur_prefixe_le_texte():
    srt = segments_to_srt([{"start": 0.0, "end": 1.0, "text": "Bonjour", "speaker": "Alice"}])
    assert "Alice: Bonjour" in srt


def test_heures_et_millisecondes():
    srt = segments_to_srt([{"start": 3661.123, "end": 3662.0, "text": "x"}])
    assert "01:01:01,123" in srt

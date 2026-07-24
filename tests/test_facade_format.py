"""Façade STT (Phase K) — sérialisation OpenAI-audio, fonctions PURES (sans Flask)."""
from transcria.web import facade_format

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": " Bonjour ", "speaker": "SPEAKER_00",
     "provenance": "final_live", "reliability": 0.9, "words": [{"w": "Bonjour"}]},
    {"start": 2.5, "end": 4.0, "text": "le monde", "speaker": "SPEAKER_01"},
    {"start": 4.0, "end": 5.0, "text": "   "},  # vide après strip → ignoré du texte
]


def test_full_text_concatene_et_ignore_les_vides():
    assert facade_format.full_text(SEGMENTS) == "Bonjour le monde"


def test_simple_json_texte_seul():
    assert facade_format.simple_json(SEGMENTS) == {"text": "Bonjour le monde"}


def test_verbose_json_structure_et_duree():
    out = facade_format.verbose_json(SEGMENTS, "fr")
    assert out["task"] == "transcribe"
    assert out["language"] == "fr"
    assert out["duration"] == 5.0            # fin du dernier segment horodaté
    assert out["text"] == "Bonjour le monde"
    assert [s["id"] for s in out["segments"]] == [0, 1, 2]


def test_verbose_json_preserve_les_champs_internes():
    seg0 = facade_format.verbose_json(SEGMENTS, "fr")["segments"][0]
    assert seg0["text"] == "Bonjour"          # trimé
    assert seg0["speaker"] == "SPEAKER_00"
    assert seg0["provenance"] == "final_live"
    assert seg0["reliability"] == 0.9
    assert seg0["words"] == [{"w": "Bonjour"}]


def test_verbose_json_pas_de_cle_fantome_si_champ_absent():
    seg1 = facade_format.verbose_json(SEGMENTS, "fr")["segments"][1]
    assert seg1["speaker"] == "SPEAKER_01"
    assert "reliability" not in seg1 and "words" not in seg1 and "provenance" not in seg1


def test_duree_zero_sans_timestamp():
    assert facade_format.verbose_json([{"text": "x"}], "fr")["duration"] == 0.0


def test_constantes_formats():
    assert facade_format.DEFAULT_RESPONSE_FORMAT == "json"
    assert set(facade_format.RESPONSE_FORMATS) == {"json", "verbose_json", "text", "srt"}

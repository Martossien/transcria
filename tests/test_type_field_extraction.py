"""Tests de l'extraction légère des champs de type (trou macro Word structuré)."""
from __future__ import annotations

from transcria.workflow.type_field_extraction import (
    build_extraction_messages,
    extract_fields_from_type,
    merge_into_structured_data,
    parse_extracted_fields,
)

_FIELDS = [
    {"key": "deliberations", "label": "Délibérations votées",
     "instruction": "les délibérations soumises au vote"},
    {"key": "absents", "label": "Absents excusés", "instruction": "les personnes excusées"},
]


class TestGating:
    def test_extract_fields_valides(self):
        assert len(extract_fields_from_type({"extract_fields": _FIELDS})) == 2

    def test_champ_sans_instruction_ignore(self):
        t = {"extract_fields": [{"key": "x"}, {"instruction": "y"}, {"key": "z", "instruction": "ok"}]}
        assert [f["key"] for f in extract_fields_from_type(t)] == ["z"]

    def test_type_none_ou_sans_champs(self):
        assert extract_fields_from_type(None) == []
        assert extract_fields_from_type({}) == []


class TestMessages:
    def test_prompt_court_contient_les_cles_et_regles(self):
        msgs = build_extraction_messages(transcript="bla", extract_fields=_FIELDS)
        system = msgs[0]["content"]
        assert "deliberations" in system and "absents" in system
        assert "ZÉRO INVENTION" in system and "JSON" in system
        assert "Transcription" in msgs[1]["content"]

    def test_transcription_tronquee(self):
        msgs = build_extraction_messages(transcript="x" * 200, extract_fields=_FIELDS,
                                         max_transcript_chars=50)
        assert "tronquée" in msgs[1]["content"]


class TestParse:
    def test_json_propre(self):
        r = '{"deliberations": ["budget 2026", "travaux école"], "absents": ["M. Durand"]}'
        out = parse_extracted_fields(r, _FIELDS)
        assert out["deliberations"] == ["budget 2026", "travaux école"]
        assert out["absents"] == ["M. Durand"]

    def test_json_dans_du_bruit_et_think(self):
        r = '<think>je réfléchis</think>\nVoici :\n{"deliberations": ["x"], "absents": []}\nfin'
        out = parse_extracted_fields(r, _FIELDS)
        assert out["deliberations"] == ["x"] and out["absents"] == []

    def test_reponse_illisible_tout_vide_jamais_exception(self):
        for bad in ("", "pas de json", "{cassé", None):
            out = parse_extracted_fields(bad, _FIELDS)
            assert out == {"deliberations": [], "absents": []}

    def test_cles_hors_perimetre_ignorees(self):
        r = '{"deliberations": ["a"], "inventé": ["ne doit pas passer"]}'
        out = parse_extracted_fields(r, _FIELDS)
        assert set(out.keys()) == {"deliberations", "absents"}
        assert "inventé" not in out

    def test_valeur_chaine_convertie_en_liste(self):
        out = parse_extracted_fields('{"deliberations": "une seule", "absents": []}', _FIELDS)
        assert out["deliberations"] == ["une seule"]


class TestMerge:
    def test_fusion_ajoute_les_champs_non_vides(self):
        sd = {"decisions": ["d1"]}
        merged, added = merge_into_structured_data(sd, {"deliberations": ["x"], "absents": []})
        assert merged["decisions"] == ["d1"]      # existant préservé
        assert merged["deliberations"] == ["x"]   # ajouté
        assert "absents" not in merged            # vide non ajouté
        assert added == ["deliberations"]

    def test_ne_pas_ecraser_un_champ_existant_non_vide(self):
        sd = {"deliberations": ["déjà là"]}
        merged, added = merge_into_structured_data(sd, {"deliberations": ["autre"]})
        assert merged["deliberations"] == ["déjà là"]
        assert added == []

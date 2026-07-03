"""Catalogue des types de réunion en données (lot A — docs/TYPES_REUNION_PERSONNALISES.md).

Trois responsabilités :
  1. NON-RÉGRESSION : les littéraux ci-dessous sont l'instantané des anciennes
     constantes de code (MEETING_TYPES, TYPE_SPECIFIC_FIELDS, _THEMES, _CSE_TYPES,
     _AUTO_CONFIDENTIEL) — le catalogue YAML doit produire EXACTEMENT les mêmes vues.
  2. GARDE ANTI-HARDCODE : plus aucun type/thème/champ en dur dans les modules
     consommateurs (même motif que la garde de llm_profiles).
  3. VALIDATEUR : ``validate_type_definition`` est le contrat d'entrée des types
     personnalisés (lot B) et de l'import communautaire — refus explicites, jamais
     de nettoyage silencieux.
"""
from pathlib import Path

import pytest

from transcria.context.meeting_type_catalog import (
    MeetingTypeCatalogError,
    confidential_types,
    detection_hints,
    load_builtin_types,
    meeting_type_names,
    quorum_types,
    theme_specs,
    type_specific_fields,
    validate_type_definition,
)

_REPO = Path(__file__).resolve().parent.parent

# Instantané des anciennes constantes (l'ordre est celui du menu de l'étape 4).
EXPECTED_TYPES = [
    "Réunion interne", "Réunion projet", "Réunion technique", "Formation",
    "Réunion médicale / santé", "RH", "Entretien",
    "CSE", "CSE extraordinaire", "CODIR / COMEX", "Réunion client", "Point projet",
    "Réunion de crise", "Séminaire / atelier", "Négociation", "Entretien individuel",
    "Podcast / média", "Autre",
]
EXPECTED_THEMED = sorted(set(EXPECTED_TYPES) - {"Réunion interne", "Autre"})
EXPECTED_WITH_FIELDS = {
    "CSE", "CSE extraordinaire", "CODIR / COMEX", "Réunion client", "Point projet",
    "Entretien individuel", "Formation", "Réunion de crise", "Séminaire / atelier",
    "Négociation",
}


class TestNonRegression:
    def test_les_18_types_dans_l_ordre_du_menu(self):
        assert meeting_type_names() == EXPECTED_TYPES

    def test_les_16_types_a_theme(self):
        assert sorted(theme_specs()) == EXPECTED_THEMED

    def test_types_a_champs_specifiques(self):
        assert set(type_specific_fields()) == EXPECTED_WITH_FIELDS

    def test_drapeaux_de_comportement(self):
        assert quorum_types() == frozenset({"CSE", "CSE extraordinaire"})
        assert confidential_types() == frozenset(
            {"Entretien individuel", "RH", "Réunion médicale / santé"}
        )

    def test_theme_cse_transcrit_a_l_identique(self):
        cse = theme_specs()["CSE"]
        assert cse["palette"] == {"primary": "1A237E", "accent": "303F9F", "light": "E8EAF6"}
        assert cse["banner_text"] == "PROCÈS-VERBAL DU COMITÉ SOCIAL ET ÉCONOMIQUE"
        assert cse["badge"] == "CSE"

    def test_theme_crise_transcrit_a_l_identique(self):
        crise = theme_specs()["Réunion de crise"]
        assert crise["palette"]["primary"] == "B71C1C"
        assert crise["badge"] == "CRISE"

    def test_champs_cse_transcrits_a_l_identique(self):
        fields = type_specific_fields()["CSE"]
        assert fields[0] == {"key": "president_seance", "label": "Président de séance", "type": "text"}
        assert [f["key"] for f in fields] == [
            "president_seance", "secretaire_seance", "membres_presents",
            "membres_total", "ref_pv_precedent",
        ]

    def test_indices_de_detection_transcrits_du_prompt(self):
        hints = detection_hints()
        # Les 8 types dont le § 8 du prompt de résumé donne des indices de sélection.
        assert set(hints) == {
            "CSE", "CSE extraordinaire", "CODIR / COMEX", "Réunion client",
            "Point projet", "Réunion de crise", "Entretien individuel", "Podcast / média",
        }
        assert "comité social" in hints["CSE"]
        assert "incident" in hints["Réunion de crise"]

    def test_consommateurs_derives_du_catalogue(self):
        """meeting_context et docx_report exposent les MÊMES vues (compat importeurs)."""
        from transcria.context.meeting_context import MEETING_TYPES, TYPE_SPECIFIC_FIELDS
        from transcria.exports.docx_report import _AUTO_CONFIDENTIEL, _CSE_TYPES, _THEMES

        assert MEETING_TYPES == meeting_type_names()
        assert TYPE_SPECIFIC_FIELDS == type_specific_fields()
        assert sorted(_THEMES) == sorted(theme_specs())
        assert _CSE_TYPES == quorum_types()
        assert _AUTO_CONFIDENTIEL == confidential_types()

    def test_theme_docx_couleurs_identiques(self):
        from transcria.exports.docx_report import _THEMES

        cse = _THEMES["CSE"]
        assert str(cse.primary) == "1A237E" and str(cse.accent) == "303F9F" and str(cse.light) == "E8EAF6"


class TestGardeAntiHardcode:
    """Plus de types/thèmes/champs littéraux dans les consommateurs (source = YAML)."""

    def test_meeting_context_sans_litteraux(self):
        src = (_REPO / "transcria" / "context" / "meeting_context.py").read_text(encoding="utf-8")
        assert "president_seance" not in src
        assert "CSE extraordinaire" not in src
        assert "Négociation" not in src

    def test_docx_report_sans_litteraux(self):
        src = (_REPO / "transcria" / "exports" / "docx_report.py").read_text(encoding="utf-8")
        assert "PROCÈS-VERBAL" not in src
        assert "CSE extraordinaire" not in src
        assert "0x1A, 0x23, 0x7E" not in src  # ancienne palette CSE

    def test_le_catalogue_existe_et_est_versionne(self):
        assert (_REPO / "transcria" / "data" / "meeting_types.yaml").is_file()


class TestValidateur:
    """Contrat d'entrée des types personnalisés (lot B) et de l'import communautaire."""

    def _valid(self) -> dict:
        return {
            "name": "COMEX Société X",
            "badge": "COMEX",
            "banner_text": "COMPTE-RENDU — COMITÉ EXÉCUTIF",
            "palette": {"primary": "1c1c1c", "accent": "424242", "light": "F5F5F5"},
            "behavior": {"confidential": True},
            "fields": [{"key": "filiale", "label": "Filiale concernée", "type": "text"}],
            "detection_hints": ["comité exécutif"],
        }

    def test_type_valide_normalise(self):
        t = validate_type_definition(self._valid())
        assert t["palette"]["primary"] == "1C1C1C"  # hex normalisé en majuscules
        assert t["behavior"] == {"quorum": False, "confidential": True}
        assert t["fields"][0]["key"] == "filiale"

    def test_minimal_valide(self):
        t = validate_type_definition({"name": "Simple"})
        assert t["palette"] is None and t["fields"] == [] and t["detection_hints"] == []

    @pytest.mark.parametrize("mutation, motif", [
        ({"name": ""}, "name"),
        ({"name": "x" * 81}, "name"),
        ({"inconnu": 1}, "clés inconnues"),
        ({"badge": "BEAUCOUP TROP LONG POUR UN BADGE"}, "badge"),
        ({"banner_text": "ligne1\nligne2", "palette": None}, "ligne"),
        ({"palette": {"primary": "GGGGGG", "accent": "424242", "light": "F5F5F5"}}, "hex"),
        ({"palette": {"primary": "1C1C1C"}}, "palette"),
        ({"palette": {"primary": "1C1C1C", "accent": "424242", "light": "F5F5F5"}, "banner_text": None}, "banner_text"),
        ({"behavior": {"autre": True}}, "behavior"),
        ({"fields": [{"key": "Filiale", "label": "x", "type": "text"}]}, "clé de champ"),
        ({"fields": [{"key": "a", "label": "x", "type": "date"}]}, "type de champ"),
        ({"fields": [{"key": "a", "label": "x", "type": "text"},
                     {"key": "a", "label": "y", "type": "text"}]}, "dupliquée"),
        ({"fields": [{"key": "a", "label": "x"}]}, "key/label/type"),
        ({"detection_hints": ["x" * 201]}, "indice"),
        ({"detection_hints": [f"h{i}" for i in range(9)]}, "detection_hints"),
    ])
    def test_refus_explicites(self, mutation: dict, motif: str):
        raw = self._valid()
        raw.update(mutation)
        if mutation.get("palette", "sentinelle") is None:
            raw.pop("palette")
        if mutation.get("banner_text", "sentinelle") is None:
            raw.pop("banner_text")
        with pytest.raises(MeetingTypeCatalogError, match=motif):
            validate_type_definition(raw)

    def test_non_dict_refuse(self):
        with pytest.raises(MeetingTypeCatalogError):
            validate_type_definition(["liste"])


class TestChargementFailLoud:
    def test_catalogue_integre_charge_et_cache(self):
        types = load_builtin_types()
        assert len(types) == 18
        assert load_builtin_types() is types  # lru_cache

    def test_schema_version_verifie(self, tmp_path, monkeypatch):
        import transcria.context.meeting_type_catalog as catalog

        bad = tmp_path / "meeting_types.yaml"
        bad.write_text("schema_version: 99\ntypes:\n  - name: X\n", encoding="utf-8")
        monkeypatch.setattr(catalog, "_BUILTIN_PATH", bad)
        catalog.load_builtin_types.cache_clear()
        try:
            with pytest.raises(MeetingTypeCatalogError, match="schema_version"):
                catalog.load_builtin_types()
        finally:
            catalog.load_builtin_types.cache_clear()

    def test_doublon_refuse(self, tmp_path, monkeypatch):
        import transcria.context.meeting_type_catalog as catalog

        dup = tmp_path / "meeting_types.yaml"
        dup.write_text(
            "schema_version: 1\ntypes:\n  - name: X\n  - name: X\n", encoding="utf-8"
        )
        monkeypatch.setattr(catalog, "_BUILTIN_PATH", dup)
        catalog.load_builtin_types.cache_clear()
        try:
            with pytest.raises(MeetingTypeCatalogError, match="dupliqués"):
                catalog.load_builtin_types()
        finally:
            catalog.load_builtin_types.cache_clear()


class TestExtractFields:
    """Lot D : champs d'extraction — bornes anti-injection strictes."""

    def _base(self) -> dict:
        return {"name": "Type X", "extract_fields": [
            {"key": "budgets_evoques", "label": "Budgets évoqués",
             "instruction": "montants budgétaires explicitement cités"},
        ]}

    def test_valide_et_normalise(self):
        t = validate_type_definition(self._base())
        assert t["extract_fields"][0]["key"] == "budgets_evoques"

    @pytest.mark.parametrize("entry, motif", [
        ({"key": "decisions", "label": "x", "instruction": "y"}, "réservée"),
        ({"key": "budgets", "label": "x", "instruction": 'avec "guillemets"'}, "interdit"),
        ({"key": "budgets", "label": "x", "instruction": "avec {accolade}"}, "interdit"),
        ({"key": "budgets", "label": "x", "instruction": "avec `backtick`"}, "interdit"),
        ({"key": "Budgets", "label": "x", "instruction": "y"}, "invalide"),
        ({"key": "budgets", "label": "x"}, "exactement"),
    ])
    def test_refus(self, entry: dict, motif: str):
        with pytest.raises(MeetingTypeCatalogError, match=motif):
            validate_type_definition({"name": "Type X", "extract_fields": [entry]})

    def test_trop_de_champs_refuse(self):
        entries = [{"key": f"cle_{i}", "label": "x", "instruction": "y"} for i in range(7)]
        with pytest.raises(MeetingTypeCatalogError, match="extract_fields"):
            validate_type_definition({"name": "Type X", "extract_fields": entries})

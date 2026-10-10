"""Lignes « Participants probables » : nom, fonction, rôle — tolérant aux surprises des LLM.

Vu en réel (retours utilisateurs, 2026-10) : la LLM écrit « Prénom Nom — Fonction » entre les
crochets du gabarit ; le champ Nom de l'étape 5 recevait les deux et la fonction restait vide.
Chaque cas ci-dessous est une forme réellement produite par une LLM ou plausible selon le
modèle (gras Markdown, parenthèses, deux-points collé, id en minuscules…).
"""
from __future__ import annotations

import pytest

from transcria.llm_tools.llm_parsing import (
    parse_participant_entry,
    parse_participant_line,
    parse_structured_summary,
    split_label_function,
)
from transcria.workflow import speaker_projection as sp


class TestSplitLabelFunction:
    @pytest.mark.parametrize("label, expected", [
        ("Prénom Nom — Directeur", ("Prénom Nom", "Directeur")),
        ("Prénom Nom – Chef de projet", ("Prénom Nom", "Chef de projet")),
        ("Prénom Nom - Juriste", ("Prénom Nom", "Juriste")),
        ("Prénom Nom—DRH", ("Prénom Nom", "DRH")),                 # tiret long collé
        ("Prénom Nom (Responsable qualité)", ("Prénom Nom", "Responsable qualité")),
        ("Prénom Nom, Trésorière", ("Prénom Nom", "Trésorière")),
        ("Jean-Pierre Nom — Élu", ("Jean-Pierre Nom", "Élu")),     # tiret court collé = pas un séparateur
    ])
    def test_separateurs_reconnus(self, label, expected):
        assert split_label_function(label) == expected

    @pytest.mark.parametrize("label", ["Vendeur / fromager", "Cliente", "Jean-Pierre Nom", "Animateur", ""])
    def test_sans_separateur_tout_reste_dans_le_nom(self, label):
        assert split_label_function(label) == (label, "")

    def test_jamais_de_partie_vide(self):
        assert split_label_function("— Directeur") == ("— Directeur", "")
        assert split_label_function("(Directeur)") == ("(Directeur)", "")


class TestParseParticipantEntry:
    def test_gabarit_nom_fonction_role(self):
        e = parse_participant_entry("- SPEAKER_00 [Prénom Nom — Directeur] : pilote la réunion")
        assert e == {"speaker_id": "SPEAKER_00", "label": "Prénom Nom", "function": "Directeur",
                     "role": "pilote la réunion"}

    def test_fonction_seule_entre_crochets(self):
        e = parse_participant_entry("- SPEAKER_01 [Cliente] : pose des questions")
        assert (e["label"], e["function"], e["role"]) == ("Cliente", "", "pose des questions")

    @pytest.mark.parametrize("line", [
        "- **SPEAKER_02** [Prénom Nom — Chef de projet] : rappelle les délais",
        "- `SPEAKER_02` [Prénom Nom — Chef de projet]: rappelle les délais",
        "- SPEAKER_02 (Prénom Nom – Chef de projet) : rappelle les délais",
        "- speaker_02 [Prénom Nom (Chef de projet)] — rappelle les délais",
        "• SPEAKER_02 [Prénom Nom, Chef de projet] rappelle les délais",
        "- SPEAKER_02 [Prénom Nom — Chef de projet] : rappelle les délais",
    ])
    def test_variantes_selon_la_llm(self, line):
        e = parse_participant_entry(line)
        assert e == {"speaker_id": "SPEAKER_02", "label": "Prénom Nom", "function": "Chef de projet",
                     "role": "rappelle les délais"}

    def test_ancien_gabarit_sans_crochets(self):
        e = parse_participant_entry("- SPEAKER_04 : Prénom Nom — anime la séance")
        assert (e["label"], e["function"], e["role"]) == ("Prénom Nom", "", "anime la séance")
        e = parse_participant_entry("- SPEAKER_05 : encaisse")
        assert (e["label"], e["role"]) == ("", "encaisse")

    def test_piste_de_reunion(self):
        e = parse_participant_entry("- PISTE_7_S1 [Prénom Nom — Élue] : intervient sur le budget")
        assert e["speaker_id"] == "PISTE_7_S1" and e["function"] == "Élue"

    def test_nom_libre_jamais_un_locuteur(self):
        assert parse_participant_entry("- Alice : anime la réunion")["speaker_id"] is None
        assert parse_participant_entry("")["speaker_id"] is None

    def test_facade_historique(self):
        assert parse_participant_line("- SPEAKER_00 [Prénom Nom — Directeur] : pilote") == (
            "SPEAKER_00", "Prénom Nom", "pilote")


class TestStructuredSummaryCarriesFunction:
    def test_la_fonction_arrive_dans_speaker_roles(self):
        text = ("# Résumé de contrôle\n\n## Participants probables\n"
                "- SPEAKER_00 [Prénom Nom — Directeur] : pilote la réunion\n"
                "- SPEAKER_01 [Cliente] : pose des questions\n\n## Synthèse\nTexte.\n")
        parsed = parse_structured_summary(text)
        roles = parsed["speaker_roles"]
        assert roles["SPEAKER_00"] == {"label": "Prénom Nom", "function": "Directeur", "role": "pilote la réunion"}
        assert roles["SPEAKER_01"]["function"] == ""


class TestProjectionCarriesFunction:
    def test_normalise_rattrape_un_label_nom_fonction_deja_stocke(self):
        # Contexte écrit par une version antérieure : nom et fonction dans le label.
        assert sp.normalize_speaker_role_info({"label": "Prénom Nom — Directrice", "role": "pilote"}) == {
            "label": "Prénom Nom", "function": "Directrice", "role": "pilote"}

    def test_fonction_explicite_prioritaire(self):
        assert sp.normalize_speaker_role_info(
            {"label": "Prénom Nom — autre", "function": "DRH", "role": "r"})["function"] == "DRH"

    def test_apply_cree_le_participant_avec_sa_fonction(self):
        proj = sp.apply_speaker_roles(
            {"SPEAKER_00": {"label": "Prénom Nom", "function": "Directeur", "role": "pilote"}}, [], {}, {})
        assert proj.created == 1
        assert proj.participants[0]["name"] == "Prénom Nom" and proj.participants[0]["function"] == "Directeur"

    def test_apply_remplit_une_fonction_vide_sans_ecraser(self):
        participants = [{"id": "p1", "name": "Prénom Nom", "function": "", "service": "", "role": ""},
                        {"id": "p2", "name": "Autre Nom", "function": "Validée", "service": "", "role": ""}]
        mapping = {"mapping": {"SPEAKER_00": {"participant_id": "p1"}, "SPEAKER_01": {"participant_id": "p2"}}}
        sp.apply_speaker_roles({
            "SPEAKER_00": {"label": "Prénom Nom — Directeur", "role": "pilote"},
            "SPEAKER_01": {"label": "Autre Nom — Stagiaire", "role": "note"},
        }, participants, mapping, {})
        assert participants[0]["function"] == "Directeur"
        assert participants[1]["function"] == "Validée"        # jamais écrasé

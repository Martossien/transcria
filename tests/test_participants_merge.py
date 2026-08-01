"""Un nom CONSTATÉ par la plateforme ne se perd pas dans une extraction automatique.

VÉCU LE 2026-08-01. « Alice Dupont », que Google avait vu dans la réunion et que
l'ingestion avait semé, s'est retrouvé remplacé par `SPEAKER_00` issu de l'extraction sur la
transcription. À l'étape de validation des locuteurs, l'interface ne pouvait donc plus
proposer le vrai nom : il avait disparu avant que l'utilisateur n'arrive.

Le contrat tient en une phrase : **l'utilisateur reste souverain, l'extraction automatique
ne l'est pas.**
"""
from __future__ import annotations

import pytest

from transcria.context.participants import (
    PLATFORM_SOURCE,
    is_label,
    merge_platform_participants,
)


def _plateforme(pid="meet_1", nom="Alice Dupont"):
    return {"id": pid, "name": nom, "source": PLATFORM_SOURCE}


def _deduit(pid="p1", nom="SPEAKER_00"):
    return {"id": pid, "name": nom, "role": "intervenant"}


class TestEtiquettes:
    @pytest.mark.parametrize("nom", ["SPEAKER_00", "speaker_1", "PISTE_9e89_S1",
                                     "LOCUTEUR-2", "", "   "])
    def test_une_etiquette_de_diarisation_n_est_PAS_un_nom(self, nom):
        assert is_label(nom)

    @pytest.mark.parametrize("nom", ["Alice Dupont", "Bob", "Jean-Pierre Speakerman"])
    def test_un_vrai_nom_est_reconnu(self, nom):
        """Y compris s'il contient « speaker » : c'est le PRÉFIXE qui étiquette."""
        assert not is_label(nom)


class TestFusion:
    def test_une_extraction_automatique_ne_PERD_pas_le_nom_constaté(self):
        """Le cas exact de l'incident : deux listes qui ne se référencent pas."""
        fusion = merge_platform_participants([_plateforme()], [_deduit()])
        assert [p["name"] for p in fusion] == ["Alice Dupont"]

    def test_les_ETIQUETTES_ne_deviennent_pas_des_participants(self):
        """Vécu : quatre « participants » affichés pour deux personnes, et les étiquettes
        entraient telles quelles dans la section Participants du compte rendu. `SPEAKER_00`
        est une VOIX à relier — l'étape de validation la prend dans `speaker_stats`."""
        existants = [_plateforme("meet_1", "Alice"), _plateforme("meet_2", "Bob")]
        fusion = merge_platform_participants(
            existants, [_deduit("p1", "SPEAKER_00"), _deduit("p2", "SPEAKER_01")])
        assert [p["name"] for p in fusion] == ["Alice", "Bob"]

    def test_un_VRAI_nom_déduit_est_conservé(self):
        """L'extraction a pu reconnaître quelqu'un que la plateforme n'a pas vu : un
        intervenant cité, ou un invité arrivé sur le poste d'un autre."""
        fusion = merge_platform_participants(
            [_plateforme()], [_deduit("p1", "SPEAKER_00"), _deduit("p2", "Claire Martin")])
        assert [p["name"] for p in fusion] == ["Alice Dupont", "Claire Martin"]

    def test_le_nom_constaté_vient_EN_TÊTE(self):
        """L'étape de validation propose les candidats dans l'ordre : le fait avant la
        déduction."""
        fusion = merge_platform_participants([_plateforme()], [_deduit()])
        assert fusion[0]["name"] == "Alice Dupont"

    def test_une_EDITION_HUMAINE_fait_foi_telle_quelle(self):
        """Dès que la liste soumise référence une entrée plateforme, c'est un humain qui
        édite : ses suppressions et ses renommages doivent passer sans discussion."""
        edite = [{"id": "meet_1", "name": "Gemini T. (renommé)"}]
        assert merge_platform_participants([_plateforme()], edite) == edite

    def test_supprimer_une_personne_constatée_reste_possible(self):
        """Corollaire : sinon une entrée fausse serait indélébile. La référence à UNE autre
        entrée plateforme suffit à prouver que la liste est éditée sciemment."""
        existants = [_plateforme("meet_1", "Alice"), _plateforme("meet_2", "Bob")]
        edite = [{"id": "meet_1", "name": "Alice"}]          # Bob retiré volontairement
        assert merge_platform_participants(existants, edite) == edite

    def test_sans_entrée_plateforme_rien_ne_change(self):
        """Le cas de tous les jobs ordinaires : un upload de fichier n'a aucun participant
        constaté, la fusion doit être transparente."""
        entrant = [_deduit(), _deduit("p2", "SPEAKER_01")]
        assert merge_platform_participants([], entrant) == entrant

    def test_une_entrée_plateforme_SANS_nom_ne_protège_rien(self):
        """Protéger un nom vide reviendrait à réintroduire une ligne inutile à chaque tour."""
        vide = [{"id": "meet_1", "name": "  ", "source": PLATFORM_SOURCE}]
        assert merge_platform_participants(vide, [_deduit()]) == [_deduit()]

    def test_une_entrée_ordinaire_n_est_PAS_protégée(self):
        """Seule l'origine plateforme confère la protection — une déduction n'est pas un
        fait, et la figer empêcherait toute correction."""
        ancien = [{"id": "p1", "name": "Alice supposée"}]
        assert merge_platform_participants(ancien, [_deduit("p9")]) == [_deduit("p9")]


class TestPersistanceDeLOrigine:
    def test_la_marque_survit_a_l_enregistrement(self, tmp_path, monkeypatch):
        """Sans cela, la PREMIÈRE réécriture ferait retomber une personne constatée au rang
        de simple proposition, et la fusion suivante ne saurait plus la protéger."""
        from transcria.context.participants import ParticipantsManager

        class _Job:
            id = "job-1"

        enregistre = {}

        class _FS:
            def __init__(self, *a):
                pass

            def load_json(self, chemin):
                return enregistre.get(chemin)

            def save_json(self, chemin, valeur):
                enregistre[chemin] = valeur

        monkeypatch.setattr("transcria.context.participants.JobFilesystem", _FS)
        ParticipantsManager.save(_Job(), str(tmp_path), [_plateforme()])
        rendu = ParticipantsManager.save(_Job(), str(tmp_path), [_deduit()])
        marques = [p for p in rendu if p.get("source") == PLATFORM_SOURCE]
        assert len(marques) == 1 and marques[0]["name"] == "Alice Dupont"

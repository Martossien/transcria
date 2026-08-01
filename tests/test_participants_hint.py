"""Indice de participants — ce qu'on sait d'une réunion dont l'audio est MIXÉ.

VÉCU LE 2026-08-01, et c'est toute la raison d'être de ce module : une réunion Meet à UN
participant a produit `SPEAKER_00` et `SPEAKER_01`. L'API Meet annonçait « 1 participant » ;
nous ne le demandions pas, et rien ne bornait pyannote, qui coupe volontiers une voix unique
en deux.

L'indice n'est PAS un manifeste. Un manifeste dit qui parle et quand — le pipeline lui fait
confiance et SAUTE la diarisation, ce qui sur un mixage donnerait zéro locuteur. L'indice dit
seulement combien de personnes étaient là, et comment elles s'appellent.
"""
from __future__ import annotations

import pytest

from connector_service.meet_api_client import participant_names
from transcria.ingestion.participants_hint import (
    MAX_PARTICIPANTS,
    ParticipantsHintError,
    parse_hint,
    seed_entries,
    speaker_hint,
)


class TestLecture:
    def test_noms_et_nombre(self):
        assert parse_hint({"names": ["Alice", "Bob"], "count": 2}) == (["Alice", "Bob"], 2)

    def test_le_NOMBRE_prime_sur_la_liste(self):
        """Une réunion compte des participants anonymes ou par téléphone, sans nom
        exploitable, qui parlent tout de même : retenir la seule liste nommée
        sous-estimerait les voix à chercher."""
        assert parse_hint({"names": ["Alice"], "count": 3}) == (["Alice"], 3)

    def test_un_nombre_INFERIEUR_a_la_liste_est_relevé(self):
        """L'inverse n'a pas de sens : trois noms, c'est au moins trois voix."""
        assert parse_hint({"names": ["A", "B", "C"], "count": 1})[1] == 3

    def test_les_doublons_et_les_vides_sont_ecartes(self):
        noms, compte = parse_hint({"names": ["Alice", "  ", "Alice", "Bob"]})
        assert noms == ["Alice", "Bob"] and compte == 2

    @pytest.mark.parametrize("brut", [
        None, "texte", {"names": "pas une liste"}, {"count": "beaucoup"},
        {"names": []}, {"count": 0}, {"count": MAX_PARTICIPANTS + 1},
    ])
    def test_un_indice_INEXPLOITABLE_est_refusé(self, brut):
        """Il vient d'une plateforme tierce : il informe, il ne commande pas. Une valeur
        aberrante doit être écartée — l'ingestion continue sans, comme pour un fichier
        audio ordinaire."""
        with pytest.raises(ParticipantsHintError):
            parse_hint(brut)


class TestFourchette:
    def test_les_bornes_sont_STRICTES(self):
        """Contrairement au manifeste, où un micro de salle peut cacher plusieurs personnes :
        chaque participant Meet est une connexion distincte. S'ils sont trois, il y a trois
        voix — ni deux, ni six. C'est cette exactitude qui empêche la sur-segmentation."""
        assert speaker_hint(3) == {"min": 3, "max": 3}

    def test_un_seul_participant_borne_la_diarisation_a_UNE_voix(self):
        """Le cas exact de l'incident : une voix unique coupée en deux."""
        assert speaker_hint(1) == {"min": 1, "max": 1}


class TestSemis:
    def test_les_noms_deviennent_des_participants_attendus(self):
        """`expected=True` : ces personnes étaient RÉELLEMENT là, la plateforme les a vues.
        C'est plus fort qu'une liste d'invités."""
        entrees = seed_entries(["Alice Dupont", "Bob Morane"])
        assert [e["name"] for e in entrees] == ["Alice Dupont", "Bob Morane"]
        assert all(e["expected"] for e in entrees)
        assert len({e["id"] for e in entrees}) == 2      # identifiants distincts

    def test_aucun_nom_ne_seme_rien(self):
        assert seed_entries([]) == []


class TestExtractionMeet:
    """Trois formes d'identité coexistent chez Meet, et il faut les trois."""

    def test_les_trois_familles_de_participants_sont_lues(self):
        participants = [
            {"signedinUser": {"displayName": "Alice", "user": "users/1"}},
            {"anonymousUser": {"displayName": "Invité externe"}},
            {"phoneUser": {"displayName": "+33 6 12 34 56 78"}},
        ]
        assert participant_names(participants) == ["Alice", "Invité externe",
                                                   "+33 6 12 34 56 78"]

    def test_n_en_lire_qu_une_perdrait_des_personnes(self):
        """Et fausserait le NOMBRE de voix annoncé — justement ce qui borne la diarisation."""
        assert len(participant_names([{"anonymousUser": {"displayName": "X"}},
                                      {"phoneUser": {"displayName": "Y"}}])) == 2

    def test_un_participant_SANS_nom_est_ignore(self):
        assert participant_names([{"signedinUser": {"user": "users/1"}}, {}]) == []

    def test_le_meme_nom_deux_fois_ne_compte_qu_une(self):
        """Une personne qui se reconnecte apparaît deux fois : la compter double ferait
        chercher une voix de plus qu'il n'y en a."""
        assert participant_names([{"signedinUser": {"displayName": "Alice"}},
                                  {"signedinUser": {"displayName": "Alice"}}]) == ["Alice"]

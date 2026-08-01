"""Ce que l'administrateur colle dans « Surveiller ».

Le champ acceptait tout, et le service découvrait l'erreur au tour suivant — loin du geste
qui l'avait causée. Vécu le 2026-08-01 : une PORTÉE OAuth collée dans le champ, juste après
l'avoir ajoutée dans la console Google. Elle est restée en configuration, muette, et rien ne
surveillait la réunion qu'on croyait avoir ajoutée.
"""
from __future__ import annotations

import pytest

from transcria.ingestion.meet_links import MeetLinkError, normalize_meeting_input


class TestFormesAdmises:
    @pytest.mark.parametrize("saisi,attendu", [
        ("https://meet.google.com/abc-mnop-xyz", "https://meet.google.com/abc-mnop-xyz"),
        ("https://meet.google.com/abc-mnop-xyz?authuser=5",
         "https://meet.google.com/abc-mnop-xyz"),
        ("  https://meet.google.com/abc-mnop-xyz  ", "https://meet.google.com/abc-mnop-xyz"),
        ("abc-mnop-xyz", "abc-mnop-xyz"),
        ("spaces/aB3dEfGh7JkL", "spaces/aB3dEfGh7JkL"),
    ])
    def test_les_trois_formes_que_l_exploitant_a_sous_la_main(self, saisi, attendu):
        assert normalize_meeting_input(saisi) == attendu

    def test_la_forme_SAISIE_est_conservee_pas_reduite(self):
        """L'administrateur relit cette liste : remplacer son lien par un identifiant opaque
        l'empêcherait de reconnaître sa propre réunion."""
        assert normalize_meeting_input("https://meet.google.com/abc-mnop-xyz") \
            .startswith("https://")


class TestRefus:
    def test_une_PORTEE_OAuth_est_refusee_et_expliquee(self):
        """L'erreur RÉELLE : on vient d'ajouter une portée dans la console Google, et on la
        colle dans le champ le plus proche. Le message doit renvoyer au bon endroit."""
        with pytest.raises(MeetLinkError) as exc:
            normalize_meeting_input("https://www.googleapis.com/auth/meetings.space.settings")
        assert "PORTÉE OAuth" in str(exc.value)
        assert "Délégation" in str(exc.value)

    def test_un_lien_d_une_AUTRE_plateforme_est_refuse(self):
        """Meet est le seul connecteur qui se pilote ainsi : pour Jitsi/Visio/Zoom, c'est
        l'utilisateur qui planifie depuis le portail."""
        with pytest.raises(MeetLinkError, match="Google Meet"):
            normalize_meeting_input("https://meet.jit.si/ma-salle")

    def test_un_lien_meet_SANS_code_valide_est_refuse(self):
        with pytest.raises(MeetLinkError, match="code de réunion"):
            normalize_meeting_input("https://meet.google.com/lookup/xyz")

    @pytest.mark.parametrize("saisi", ["", "   ", "n'importe quoi", "abc-defg", "12345"])
    def test_les_formes_non_reconnues_sont_refusees(self, saisi):
        with pytest.raises(MeetLinkError):
            normalize_meeting_input(saisi)

    def test_le_message_donne_TOUJOURS_un_exemple(self):
        """Un refus sans exemple renvoie à la documentation ; avec, il se corrige seul."""
        for saisi in ("n'importe quoi", "https://meet.google.com/lookup/xyz",
                      "https://www.googleapis.com/auth/x"):
            with pytest.raises(MeetLinkError) as exc:
                normalize_meeting_input(saisi)
            assert "abc-mnop-xyz" in str(exc.value)

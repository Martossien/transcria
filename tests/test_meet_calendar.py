"""Découverte des réunions à venir dans l'agenda, et pré-réglage des salles.

C'est la pièce qui tient la promesse « l'utilisateur ne fait rien » SANS changer d'édition
Workspace : le réglage d'organisation « enregistrées par défaut » n'existe qu'à partir de
Business Plus, l'agenda est le seul moyen d'apprendre à l'avance quelles salles vont servir.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from connector_service.meet_calendar import (
    discover_and_prepare,
    horizon,
    meeting_links,
    upcoming_call,
)

MAINTENANT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
LIEN = "https://meet.google.com/abc-mnop-xyz"


class TestRequete:
    def test_les_series_recurrentes_sont_DEPLIEES(self):
        """Sans `singleEvents`, une réunion hebdomadaire n'apparaît qu'une fois avec la date
        de sa PREMIÈRE occurrence — on préparerait la salle d'une réunion déjà passée."""
        _, url, _ = upcoming_call(time_min="A", time_max="B")
        assert "singleEvents=true" in url and "orderBy=startTime" in url

    def test_le_nombre_de_resultats_est_borne(self):
        """Un agenda chargé n'est pas une raison de tirer mille évènements par tour."""
        assert "maxResults=2500" in upcoming_call(time_min="A", time_max="B",
                                                  max_results=99999)[1]
        assert "maxResults=1" in upcoming_call(time_min="A", time_max="B", max_results=0)[1]

    def test_l_horizon_part_de_l_instant_INJECTE(self):
        """Aucune lecture d'horloge dans le module : les cas de bord doivent être rejouables."""
        debut, fin = horizon(MAINTENANT, days=7)
        assert debut.startswith("2026-08-01") and fin.startswith("2026-08-08")


class TestExtraction:
    def test_le_lien_direct_est_lu(self):
        assert meeting_links({"items": [{"hangoutLink": LIEN}]}) == [LIEN]

    def test_la_forme_MODERNE_est_lue_aussi(self):
        """`conferenceData.entryPoints` est parfois la seule renseignée (évènements créés par
        API) : n'en lire qu'une laisse passer des réunions sans que rien ne le signale."""
        charge = {"items": [{"conferenceData": {"entryPoints": [
            {"entryPointType": "video", "uri": LIEN},
            {"entryPointType": "phone", "uri": "tel:+331"}]}}]}
        assert meeting_links(charge) == [LIEN]

    def test_les_AUTRES_plateformes_sont_ecartees(self):
        """Demander à Google de régler une salle Zoom n'a aucun sens — et produirait une
        erreur par réunion, à chaque tour."""
        charge = {"items": [{"conferenceData": {"entryPoints": [
            {"entryPointType": "video", "uri": "https://zoom.us/j/123"},
            {"entryPointType": "video", "uri": "https://teams.microsoft.com/l/x"}]}}]}
        assert meeting_links(charge) == []

    def test_une_meme_salle_n_apparait_qu_UNE_fois(self):
        """Une réunion hebdomadaire dépliée donne dix occurrences de la même salle : la
        régler dix fois serait dix appels pour un seul effet."""
        charge = {"items": [{"hangoutLink": LIEN}, {"hangoutLink": LIEN},
                            {"conferenceData": {"entryPoints": [
                                {"entryPointType": "video", "uri": LIEN}]}}]}
        assert meeting_links(charge) == [LIEN]

    @pytest.mark.parametrize("charge", [None, {}, {"items": []}, {"items": [None, "x"]},
                                        {"items": [{"summary": "sans visio"}]}])
    def test_un_agenda_sans_meet_ne_rend_RIEN(self, charge):
        assert meeting_links(charge) == []


class _FauxSalles:
    def __init__(self, refuse=()):
        self.regles: list[str] = []
        self._refuse = set(refuse)

    def resolve_space(self, lien):
        return "spaces/" + lien.rsplit("/", 1)[-1]

    def set_auto_recording(self, espace, *, enabled=True):
        if espace in self._refuse:
            raise RuntimeError("403 réglage refusé")
        self.regles.append(espace)
        return "ON"


class TestPreparation:
    def _agenda(self, par_utilisateur, casse=()):
        def appel(adresse, methode, url):
            if adresse in casse:
                raise RuntimeError("agenda privé")
            return {"items": [{"hangoutLink": lien}
                              for lien in par_utilisateur.get(adresse, [])]}
        return appel

    def test_les_salles_a_venir_sont_reglees(self):
        salles = _FauxSalles()
        bilan = discover_and_prepare(
            users=["a@x.test"], now=MAINTENANT,
            calendar_call=self._agenda({"a@x.test": [LIEN]}), settings_client=salles)
        assert bilan["prepared"] == [LIEN]
        assert salles.regles == ["spaces/abc-mnop-xyz"]

    def test_un_agenda_ILLISIBLE_n_empeche_pas_les_autres(self):
        """Le bug de boucle classique, ici à l'échelle de l'organisation : une personne dont
        l'agenda est privé priverait tout le monde de préparation."""
        salles = _FauxSalles()
        bilan = discover_and_prepare(
            users=["prive@x.test", "ok@x.test"], now=MAINTENANT,
            calendar_call=self._agenda({"ok@x.test": [LIEN]}, casse=("prive@x.test",)),
            settings_client=salles)
        assert bilan["prepared"] == [LIEN]
        assert any("prive@x.test" in e for e in bilan["failed"])

    def test_une_salle_qui_REFUSE_est_signalee_sans_bloquer(self):
        salles = _FauxSalles(refuse=("spaces/abc-mnop-xyz",))
        bilan = discover_and_prepare(
            users=["a@x.test"], now=MAINTENANT,
            calendar_call=self._agenda({"a@x.test": [LIEN]}), settings_client=salles)
        assert bilan["prepared"] == [] and len(bilan["failed"]) == 1

    def test_aucun_utilisateur_ne_fait_AUCUN_appel(self):
        salles = _FauxSalles()
        appels = []
        discover_and_prepare(users=[], now=MAINTENANT,
                             calendar_call=lambda *a: appels.append(a) or {},
                             settings_client=salles)
        assert appels == [] and salles.regles == []

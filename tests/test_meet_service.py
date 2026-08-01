"""Service Meet — la boucle permanente, et ce qu'elle doit survivre.

Ce que ces tests protègent n'est pas le chemin nominal (couvert par `test_meet_poller` et
`test_meet_keeper`) mais la RÉSILIENCE : un service supervisé qui plante au démarrage entre
en boucle de redémarrage, et un tour raté ne doit pas tuer les suivants.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from connector_service.meet_service import (
    SUBSCRIPTION_FILTER,
    MeetNotConfigured,
    MeetService,
    MeetServiceConfig,
)

IDENTITES = {
    "MEET_SERVICE_ACCOUNT_JSON": json.dumps({"client_email": "svc@x.iam.gserviceaccount.com",
                                             "private_key": "-----BEGIN PRIVATE KEY-----"}),
    "MEET_IMPERSONATE_USER": "admin@exemple.test",
    "MEET_PUBSUB_SUBSCRIPTION": "projects/p/subscriptions/s",
}


def _config(**surcharges) -> MeetServiceConfig:
    return MeetServiceConfig.from_identities(
        {**IDENTITES, **surcharges.pop("identities", {})},
        portal_url="http://127.0.0.1:7870", portal_token="tia_x", **surcharges)


class _FauxSondeur:
    def __init__(self, *, echoue=False):
        self.tours = 0
        self._echoue = echoue

    async def poll_once(self):
        self.tours += 1
        if self._echoue:
            raise RuntimeError("Pub/Sub injoignable")
        from connector_service.meet_poller import PollOutcome
        return PollOutcome(pulled=0)


class _FauxGardien:
    def __init__(self, *, echoue=False):
        self.tours = 0
        self._echoue = echoue

    def keep_once(self, now=None):
        self.tours += 1
        if self._echoue:
            raise RuntimeError("Workspace Events injoignable")
        from connector_service.meet_keeper import KeepOutcome
        return KeepOutcome(inspected=1)


class TestConfiguration:
    @pytest.mark.parametrize("manquante", list(IDENTITES))
    def test_chaque_manque_est_NOMME(self, manquante):
        """« Configuration incomplète » obligerait l'exploitant à deviner lequel des trois
        champs il a oublié."""
        identites = {k: v for k, v in IDENTITES.items() if k != manquante}
        with pytest.raises(MeetNotConfigured, match=manquante):
            MeetServiceConfig.from_identities(identites, portal_url="u", portal_token="t")

    def test_le_jeton_du_portail_manquant_renvoie_au_BOUTON(self):
        with pytest.raises(MeetNotConfigured, match="activer les réunions"):
            MeetServiceConfig.from_identities(IDENTITES, portal_url="u", portal_token="")

    def test_une_cle_illisible_dit_de_la_redeposer(self):
        with pytest.raises(MeetNotConfigured, match="redéposer"):
            MeetServiceConfig.from_identities(
                {**IDENTITES, "MEET_SERVICE_ACCOUNT_JSON": "/chemin/absent.json"},
                portal_url="u", portal_token="t")

    def test_le_contenu_JSON_colle_est_accepte_comme_un_chemin(self):
        assert _config().service_account["client_email"].startswith("svc@")

    def test_le_filtre_d_abonnements_porte_l_evenement_declencheur(self):
        """Un filtre erroné n'échoue pas : il maintient simplement les MAUVAIS abonnements,
        et les bons expirent en silence."""
        assert "recording.v2.fileGenerated" in SUBSCRIPTION_FILTER


class TestBoucle:
    def test_une_fiche_INCOMPLETE_endort_le_service_au_lieu_de_le_tuer(self):
        """Une unité systemd qui plante au démarrage entre en boucle de redémarrage et noie
        le journal — sans rien réparer. Le meeting-runner dort de la même façon."""
        dodos = []

        def charger():
            raise MeetNotConfigured("MEET_IMPERSONATE_USER absent")

        service = MeetService(charger, build=lambda c: (None, None),
                              sleep=lambda d: dodos.append(d) or asyncio.sleep(0))
        tours = asyncio.run(service.run_forever(max_cycles=3))
        assert tours == 3 and len(dodos) == 3

    def test_le_maintien_precede_le_sondage(self):
        """Un abonnement expiré ne délivre plus rien : découvrir au bout d'une heure qu'on
        interrogeait une file condamnée, c'est une heure perdue."""
        ordre = []
        sondeur, gardien = _FauxSondeur(), _FauxGardien()

        class _Trace:
            def __init__(self, nom, delegue):
                self._nom, self._delegue = nom, delegue

            async def poll_once(self):
                ordre.append(self._nom)
                return await self._delegue.poll_once()

            def keep_once(self, now=None):
                ordre.append(self._nom)
                return self._delegue.keep_once()

        service = MeetService(lambda: _config(),
                              build=lambda c: (_Trace("sondage", sondeur),
                                               _Trace("maintien", gardien)),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=1))
        assert ordre == ["maintien", "sondage"]

    def test_le_sondeur_n_est_PAS_reconstruit_a_chaque_tour(self):
        """Le reconstruire perdait sa mémoire des ingestions déjà faites : un évènement
        redélivré était re-téléchargé de Drive et re-téléversé au portail — un enregistrement
        complet transféré deux fois, pour finir rejeté par l'idempotence serveur."""
        constructions = []
        service = MeetService(lambda: _config(),
                              build=lambda c: constructions.append(c) or (_FauxSondeur(),
                                                                          _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=4))
        assert len(constructions) == 1

    def test_une_configuration_MODIFIEE_reconstruit_le_sondeur(self):
        """L'administrateur corrige la fiche : le service doit en tenir compte sans qu'on le
        redémarre — c'est tout l'intérêt de relire la configuration à chaque tour."""
        tours = {"n": 0}

        def charger():
            tours["n"] += 1
            # La fiche est corrigée entre le 2ᵉ et le 3ᵉ tour.
            return _config() if tours["n"] < 3 else _config(keep_every_s=99.0)

        constructions = []
        service = MeetService(charger,
                              build=lambda c: constructions.append(c) or (_FauxSondeur(),
                                                                          _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=3))
        assert len(constructions) == 2

    def test_le_maintien_ne_repart_PAS_a_chaque_tour(self):
        """Renouveler à chaque tour de sondage martèlerait l'API pour rien : sept jours de
        marge n'appellent pas un appel par minute."""
        gardien = _FauxGardien()
        service = MeetService(lambda: _config(keep_every_s=3600.0),
                              build=lambda c: (_FauxSondeur(), gardien),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=5))
        assert gardien.tours == 1

    def test_un_maintien_en_ECHEC_n_empeche_pas_l_ingestion(self):
        """Deux dépendances distinctes, qui tombent séparément : mieux vaut ingérer avec un
        abonnement en sursis que tout arrêter."""
        sondeur = _FauxSondeur()
        service = MeetService(lambda: _config(),
                              build=lambda c: (sondeur, _FauxGardien(echoue=True)),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=2))
        assert sondeur.tours == 2

    def test_un_sondage_en_ECHEC_ne_tue_pas_le_service(self):
        sondeur = _FauxSondeur(echoue=True)
        service = MeetService(lambda: _config(),
                              build=lambda c: (sondeur, _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        assert asyncio.run(service.run_forever(max_cycles=3)) == 3
        assert sondeur.tours == 3

    def test_un_service_en_veille_DIT_qu_il_tourne(self, caplog):
        """Une file calme est le cas normal. Sans battement de cœur, le journal d'un service
        en bonne santé est identique à celui d'un service figé sur une interrogation qui ne
        revient jamais — et personne ne s'en aperçoit avant de chercher un job absent."""
        import logging

        service = MeetService(lambda: _config(heartbeat_s=0.0),
                              build=lambda c: (_FauxSondeur(), _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        with caplog.at_level(logging.INFO, logger="connector_service.meet_service"):
            asyncio.run(service.run_forever(max_cycles=1))
        assert any("en veille active" in m for m in caplog.messages)

    def test_le_battement_est_ESPACE_pas_a_chaque_tour(self, caplog):
        """Un message par tour de sondage noierait le journal — l'inverse du but."""
        import logging

        service = MeetService(lambda: _config(heartbeat_s=10_000.0),
                              build=lambda c: (_FauxSondeur(), _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        with caplog.at_level(logging.INFO, logger="connector_service.meet_service"):
            asyncio.run(service.run_forever(max_cycles=4))
        assert sum("en veille active" in m for m in caplog.messages) == 1

    def test_les_jobs_restent_affiches_apres_leur_tour(self):
        """Un job est créé en une seconde ; ne publier que ceux du tour COURANT rendait le
        panneau d'administration muet en pratique — des centaines de tours à liste vide."""
        from connector_service.bridge import IngestResult
        from connector_service.meet_poller import PollOutcome
        from connector_service.reconciler import ReconcileOutcome

        class _UnJobPuisRien(_FauxSondeur):
            async def poll_once(self):
                self.tours += 1
                if self.tours > 1:
                    return PollOutcome(pulled=0)
                return PollOutcome(pulled=1, triggering=1, ingested=[ReconcileOutcome(
                    dedup_key="k", action="imported",
                    result=IngestResult(202, "job-42", False))])

        sondeur = _UnJobPuisRien()
        service = MeetService(lambda: _config(),
                              build=lambda c: (sondeur, _FauxGardien()),
                              sleep=lambda d: asyncio.sleep(0))
        asyncio.run(service.run_forever(max_cycles=3))
        assert service._derniers_jobs == ["job-42"]
        assert sondeur.tours == 3          # le MÊME sondeur a servi aux trois tours

    def test_stop_arrete_la_boucle(self):
        sondeur = _FauxSondeur()

        class _UnTour(_FauxSondeur):
            def __init__(self, service):
                super().__init__()
                self._service = service

            async def poll_once(self):
                self._service.stop()
                return await sondeur.poll_once()

        service = MeetService(lambda: _config(), sleep=lambda d: asyncio.sleep(0))
        service._build = lambda c: (_UnTour(service), _FauxGardien())
        assert asyncio.run(service.run_forever()) == 1

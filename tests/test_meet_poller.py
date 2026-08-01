"""Sondeur Meet — les décisions d'acquittement, qui sont tout l'enjeu.

Un sondeur de file se trompe de trois façons, et aucune ne se voit avant que les évènements
ne cessent d'arriver : acquitter ce qu'on n'a pas traité (l'enregistrement est perdu), ne
pas acquitter ce qui n'appelait rien (la file se remplit et masque les vrais évènements),
laisser un message empoisonné tourner sans le dire.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from connector_service.bridge import IngestResult
from connector_service.meet_events import (
    CONFERENCE_ENDED,
    RECORDING_FILE_GENERATED,
)
from connector_service.meet_poller import MeetPoller, occurrence_of
from connector_service.pubsub_pull import PulledMessage, to_meet_event
from connector_service.reconciler import ReconcileOutcome

CR = "conferenceRecords/M5L9GK"
REC = f"{CR}/recordings/a877e4f7"


def _message(event_type: str, resource: str, *, ack_id: str = "ack-1",
             attempt: int = 1) -> PulledMessage:
    """Message tel que Pub/Sub le rend : CloudEvents binaire, charge en base64."""
    charge = base64.b64encode(json.dumps({"recording": {"name": resource}}).encode()).decode()
    return PulledMessage(ack_id=ack_id, attributes={"ce-type": event_type,
                                                    "ce-source": "//meet.googleapis.com/spaces/uZn0"},
                         data=charge, message_id=f"msg-{ack_id}", delivery_attempt=attempt)


class _FauxReconciler:
    """Reconciler minimal : note les occurrences reçues, peut échouer sur commande."""

    def __init__(self, *, echoue: bool = False) -> None:
        self.vues: list = []
        self._echoue = echoue

    async def reconcile(self, occurrence, *, already_imported=None):
        self.vues.append(occurrence)
        if self._echoue:
            raise RuntimeError("Drive injoignable")
        return [ReconcileOutcome(dedup_key="k", action="imported",
                                 result=IngestResult(status_code=201, job_id="job-42",
                                                     idempotent=False))]


def _sondeur(messages, reconciler):
    acquittes: list[tuple[str, ...]] = []

    async def pull():
        return list(messages)

    async def acknowledge(ids):
        acquittes.append(ids)

    return MeetPoller(pull=pull, acknowledge=acknowledge, reconciler=reconciler), acquittes


class TestOccurrence:
    def test_l_identifiant_est_NU_sans_prefixe(self):
        """Le provider reconstruit `conferenceRecords/{id}/recordings` : laisser le préfixe
        produirait `conferenceRecords/conferenceRecords/…`, donc un 404 opaque."""
        occurrence = occurrence_of(to_meet_event(_message(RECORDING_FILE_GENERATED, REC)))
        assert occurrence.external_occurrence_id == "M5L9GK"
        assert occurrence.provider == "meet"

    def test_l_espace_de_la_source_sert_de_compte(self):
        occurrence = occurrence_of(to_meet_event(_message(RECORDING_FILE_GENERATED, REC)))
        assert occurrence.provider_account_id == "uZn0"


class TestAcquittements:
    def test_un_enregistrement_pret_est_ingere_puis_acquitte(self):
        reconciler = _FauxReconciler()
        sondeur, acquittes = _sondeur([_message(RECORDING_FILE_GENERATED, REC)], reconciler)
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.triggering == 1
        assert resultat.jobs == ["job-42"]
        assert acquittes == [("ack-1",)]
        assert len(reconciler.vues) == 1

    def test_un_evenement_SANS_objet_est_quand_meme_acquitte(self):
        """Vécu le 2026-08-01 : un `conference.ended` laissé en suspens a été redélivré et
        a masqué le `fileGenerated` suivant, au point de le faire croire perdu."""
        reconciler = _FauxReconciler()
        sondeur, acquittes = _sondeur([_message(CONFERENCE_ENDED, CR)], reconciler)
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.triggering == 0
        assert reconciler.vues == []          # rien n'a été ingéré…
        assert acquittes == [("ack-1",)]      # …mais la file est libérée

    def test_un_traitement_ECHOUE_n_est_PAS_acquitte(self):
        """L'inverse du précédent, et la raison pour laquelle les deux cas ne peuvent pas
        partager le même code : acquitter ici perdrait l'enregistrement définitivement."""
        sondeur, acquittes = _sondeur([_message(RECORDING_FILE_GENERATED, REC)],
                                      _FauxReconciler(echoue=True))
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.failed == 1
        assert resultat.jobs == []
        assert acquittes == []                # rien d'acquitté → réessayé au prochain tour

    def test_un_message_ILLISIBLE_est_acquitte_et_signale(self):
        """Il ne deviendra jamais lisible : le garder bloquerait la file par redélivrances."""
        illisible = PulledMessage(ack_id="ack-x", attributes={}, data="", message_id="m")
        sondeur, acquittes = _sondeur([illisible], _FauxReconciler())
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.unreadable == 1
        assert acquittes == [("ack-x",)]

    def test_un_echec_n_empeche_PAS_de_traiter_les_autres(self):
        """Le plus banal des bugs de boucle : un message en échec qui arrête le tour."""
        class _Capricieux(_FauxReconciler):
            async def reconcile(self, occurrence, *, already_imported=None):
                self.vues.append(occurrence)
                if len(self.vues) == 1:
                    raise RuntimeError("premier en échec")
                return [ReconcileOutcome(dedup_key="k", action="imported",
                                         result=IngestResult(201, "job-99", False))]

        reconciler = _Capricieux()
        sondeur, acquittes = _sondeur(
            [_message(RECORDING_FILE_GENERATED, REC, ack_id="ack-1"),
             _message(RECORDING_FILE_GENERATED, REC, ack_id="ack-2")], reconciler)
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.failed == 1
        assert resultat.jobs == ["job-99"]
        assert acquittes == [("ack-2",)]      # le second seul

    def test_un_message_redelivre_en_boucle_est_SIGNALE(self):
        """Sans ce compteur, un message empoisonné tourne indéfiniment et personne ne le voit
        avant que la file ne déborde."""
        sondeur, _ = _sondeur([_message(RECORDING_FILE_GENERATED, REC, attempt=7)],
                              _FauxReconciler())
        resultat = asyncio.run(sondeur.poll_once())
        assert resultat.stuck == ["msg-ack-1"]

    def test_file_vide_ne_declenche_aucun_acquittement(self):
        """L'API refuse une liste d'acquittement vide — et un appel vide signalerait un bug
        de la boucle plutôt qu'une file calme."""
        sondeur, acquittes = _sondeur([], _FauxReconciler())
        assert asyncio.run(sondeur.poll_once()).pulled == 0
        assert acquittes == []


class TestMemoireLocale:
    def test_la_meme_reunion_deux_fois_ne_reingere_pas(self):
        """Le sondeur passe son ensemble au reconciler, qui saute ce qu'il connaît déjà."""
        vues = []

        class _Reconciler:
            async def reconcile(self, occurrence, *, already_imported=None):
                vues.append(dict(deja=len(already_imported or ())))
                (already_imported if already_imported is not None else set()).add("k")
                return []

        sondeur, _ = _sondeur([_message(RECORDING_FILE_GENERATED, REC)], _Reconciler())
        asyncio.run(sondeur.poll_once())
        asyncio.run(sondeur.poll_once())
        assert vues == [{"deja": 0}, {"deja": 1}]


@pytest.mark.parametrize("type_evenement", [CONFERENCE_ENDED, RECORDING_FILE_GENERATED])
def test_tout_message_recu_finit_traite(type_evenement):
    """Invariant : aucun message ne reste sans décision — ni acquitté par erreur, ni oublié."""
    reconciler = _FauxReconciler()
    sondeur, acquittes = _sondeur([_message(type_evenement, REC)], reconciler)
    resultat = asyncio.run(sondeur.poll_once())
    assert resultat.pulled == 1
    assert len(acquittes) == 1

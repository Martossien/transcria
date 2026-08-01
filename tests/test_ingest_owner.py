"""Rattachement d'un enregistrement importé à son ORGANISATEUR.

Le défaut que ces tests ferment : un enregistrement déposé par un connecteur appartient au
COMPTE DE SERVICE qui porte le jeton. La chaîne entière fonctionne — évènement, télé-
chargement, transcription, compte rendu — et l'organisateur ne voit le job nulle part. Ça
marche parfaitement… pour personne.

Le privilège est RESTREINT au compte de service des connecteurs : un jeton ordinaire ne doit
pas pouvoir créer un job au nom d'un autre utilisateur.
"""
from __future__ import annotations

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.meet_events import RECORDING_FILE_GENERATED, MeetEvent
from connector_service.meet_poller import occurrence_of


class TestCoteConnecteur:
    def test_l_occurrence_porte_l_organisateur(self):
        """L'utilisateur dont on suit les réunions EST leur organisateur : l'abonnement
        porte sur son espace."""
        evenement = MeetEvent(event_type=RECORDING_FILE_GENERATED,
                              resource_name="conferenceRecords/CR/recordings/R",
                              conference_record="conferenceRecords/CR")
        assert occurrence_of(evenement, "chef@exemple.test").organizer == "chef@exemple.test"

    def test_sans_organisateur_le_champ_reste_None(self):
        """`None` et chaîne vide ne se valent pas côté pont : une chaîne vide serait envoyée
        au serveur comme un champ présent mais vide."""
        evenement = MeetEvent(event_type=RECORDING_FILE_GENERATED,
                              resource_name="conferenceRecords/CR/recordings/R",
                              conference_record="conferenceRecords/CR")
        assert occurrence_of(evenement).organizer is None

    def test_le_pont_transmet_l_adresse(self):
        """Sans ce champ dans le multipart, le serveur n'a aucun moyen de savoir à qui le
        compte rendu revient."""
        import asyncio

        from connector_service.bridge import JobsApiBridge

        vu = {}

        class _Transport:
            async def request(self, method, url, *, headers, data=None, files=None):
                vu.update(data or {})
                return 202, {"job_id": "j1"}

        asyncio.run(JobsApiBridge("http://p", "tia_x", _Transport()).ingest_recording(
            b"AUDIO", "r.mp4", idempotency_key="k", provider="meet",
            owner_email="chef@exemple.test"))
        assert vu["owner_email"] == "chef@exemple.test"

    def test_le_reconciler_propage_l_organisateur_de_l_occurrence(self):
        import asyncio

        from connector_service.reconciler import ProviderReconciler

        vu = {}

        class _Provider:
            async def fetch_artifacts(self, occurrence):
                from connector_service.contract import RemoteArtifact
                return [RemoteArtifact(artifact_id="a1", storage_uri="gdrive://x",
                                       media_type="video/mp4", artifact_type="recording")]

        class _Bridge:
            async def ingest_recording(self, audio, filename, **kwargs):
                vu.update(kwargs)
                from connector_service.bridge import IngestResult
                return IngestResult(202, "j1", False)

        async def _fetch(artifact):
            return b"AUDIO", "r.mp4"

        occurrence = ExternalMeetingOccurrence(provider="meet", provider_account_id="s",
                                               external_occurrence_id="CR",
                                               organizer="chef@exemple.test")
        asyncio.run(ProviderReconciler(_Provider(), _Bridge(),
                                       fetch_audio=_fetch).reconcile(occurrence))
        assert vu["owner_email"] == "chef@exemple.test"

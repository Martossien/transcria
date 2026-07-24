"""Adaptateur Teams post-réunion — Microsoft Graph (A3, ADR-001 D8).

Teams notifie (change notification chiffrée) la création d'un `callRecording` (ou
`callTranscript`). On traduit la ressource en occurrence + artefact ; le contenu se
récupère via Graph `…/recordings/{id}/content` (Bearer) — fetcher distinct (plomberie
ultérieure). Le transcript Teams (VTT) reste un artefact AUXILIAIRE (ADR-001 D7).

⚠️ Forme d'après la doc Graph (learn.microsoft.com) ; à confirmer contre un tenant M365
réel au gate manuel. Parsing tolérant (une notification porte une liste `value`).
"""
from __future__ import annotations

from dataclasses import dataclass

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact

PROVIDER = "teams"


class TeamsNotificationError(ValueError):
    """Notification Graph invalide ou sans ressource enregistrement exploitable."""


@dataclass(frozen=True)
class TeamsRecording:
    tenant_meeting_id: str        # onlineMeeting id = occurrence
    organizer_id: str
    recording_id: str
    resource_path: str            # "communications/onlineMeetings('…')/recordings('…')"
    change_type: str

    @classmethod
    def from_notification(cls, payload: dict) -> TeamsRecording:
        """Prend une change notification Graph ({"value": [ {resource, resourceData,
        changeType}, … ]}) et en extrait la 1re ressource callRecording créée."""
        if not isinstance(payload, dict):
            raise TeamsNotificationError("notification Teams invalide (objet attendu)")
        items = payload.get("value")
        if not isinstance(items, list) or not items:
            raise TeamsNotificationError("notification Teams sans entrée 'value'")
        for item in items:
            data = (item or {}).get("resourceData") or {}
            odata = str(data.get("@odata.type") or "")
            if "callRecording" not in odata and "/recordings(" not in str(item.get("resource") or ""):
                continue
            meeting_id = str(data.get("meetingId") or "").strip()
            rec_id = str(data.get("id") or "").strip()
            if not meeting_id or not rec_id:
                continue
            return cls(
                tenant_meeting_id=meeting_id,
                organizer_id=str(data.get("meetingOrganizerId") or ""),
                recording_id=rec_id,
                resource_path=str(item.get("resource") or ""),
                change_type=str(item.get("changeType") or ""),
            )
        raise TeamsNotificationError("aucune ressource callRecording exploitable")


class TeamsRecordingAdapter:
    #: Base Graph pour construire l'URL de contenu (le fetcher y ajoutera /content + Bearer).
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def to_occurrence(self, rec: TeamsRecording) -> ExternalMeetingOccurrence:
        return ExternalMeetingOccurrence(
            provider=PROVIDER,
            provider_account_id=rec.organizer_id,
            external_occurrence_id=rec.tenant_meeting_id,
            organizer=rec.organizer_id,
        )

    def to_artifact(self, rec: TeamsRecording) -> RemoteArtifact:
        return RemoteArtifact(
            artifact_id=rec.recording_id,
            storage_uri=f"{self.GRAPH_BASE}/{rec.resource_path}/content"
            if rec.resource_path else f"graph:recording:{rec.recording_id}",
            media_type="video/mp4",
            artifact_type="recording",
        )

    def dedup_key(self, rec: TeamsRecording) -> str:
        return "|".join((PROVIDER, rec.organizer_id, rec.tenant_meeting_id, rec.recording_id))

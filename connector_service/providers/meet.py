"""Adaptateur Google Meet post-réunion — REST API v2 (A4, ADR-001 D8).

Meet dépose l'enregistrement dans le Drive de l'organisateur ; la ressource
`conferenceRecords/{cr}/recordings/{rec}` porte l'état et la référence Drive
(`driveDestination.file`/`exportUri`). On attend `state == "FILE_GENERATED"`, puis on
traduit en occurrence (le `conferenceRecord`, unique/occurrence) + artefact Drive. Le
téléchargement passe par la Drive API (fetcher distinct, plomberie ultérieure).

⚠️ Forme d'après la doc Meet REST v2 (developers.google.com) ; à confirmer contre un
Google Workspace réel au gate manuel.
"""
from __future__ import annotations

from dataclasses import dataclass

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact

PROVIDER = "meet"


class MeetRecordingError(ValueError):
    """Ressource d'enregistrement Meet invalide ou non finalisée."""


def _parse_name(name: str) -> tuple[str, str]:
    """`conferenceRecords/CR/recordings/REC` → `("CR", "REC")`."""
    parts = name.split("/")
    if len(parts) != 4 or parts[0] != "conferenceRecords" or parts[2] != "recordings":
        raise MeetRecordingError(f"nom de ressource Meet invalide: {name}")
    return parts[1], parts[3]


@dataclass(frozen=True)
class MeetRecording:
    conference_record_id: str     # occurrence (unique par instance de réunion)
    recording_id: str
    drive_file_id: str
    export_uri: str
    space: str
    start_time: str

    @classmethod
    def from_recording(cls, payload: dict) -> MeetRecording:
        if not isinstance(payload, dict):
            raise MeetRecordingError("ressource Meet invalide (objet attendu)")
        state = str(payload.get("state") or "").upper()
        if state and state != "FILE_GENERATED":
            raise MeetRecordingError(f"enregistrement Meet non finalisé (state={state})")
        cr_id, rec_id = _parse_name(str(payload.get("name") or ""))
        drive = payload.get("driveDestination") or {}
        drive_file = str(drive.get("file") or "").strip()
        if not drive_file:
            raise MeetRecordingError("driveDestination.file manquant")
        return cls(
            conference_record_id=cr_id,
            recording_id=rec_id,
            drive_file_id=drive_file,
            export_uri=str(drive.get("exportUri") or ""),
            space=str(payload.get("space") or ""),
            start_time=str(payload.get("startTime") or ""),
        )


class MeetRecordingAdapter:
    def to_occurrence(self, rec: MeetRecording) -> ExternalMeetingOccurrence:
        return ExternalMeetingOccurrence(
            provider=PROVIDER,
            provider_account_id=rec.space or rec.conference_record_id,
            external_occurrence_id=rec.conference_record_id,
            start_time=rec.start_time,
        )

    def to_artifact(self, rec: MeetRecording) -> RemoteArtifact:
        return RemoteArtifact(
            artifact_id=rec.recording_id,
            storage_uri=f"gdrive://{rec.drive_file_id}",   # Drive API (pas s3://)
            media_type="video/mp4",
            artifact_type="recording",
        )

    def dedup_key(self, rec: MeetRecording) -> str:
        return "|".join((
            PROVIDER,
            rec.space or rec.conference_record_id,
            rec.conference_record_id,
            rec.recording_id,
        ))

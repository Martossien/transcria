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

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

from connector_service.contract import (
    ExternalMeetingOccurrence,
    ProviderCapabilities,
    RemoteArtifact,
)

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


def _requests_get(url: str, *, headers: dict) -> tuple[int, dict]:
    import requests  # dép TranscrIA

    resp = requests.get(url, headers=headers, timeout=30)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


class MeetApiClient:
    """Meet REST API v2 — liste les enregistrements d'un conferenceRecord (poll /
    réconciliation). `oauth` = GoogleOAuth ; `http_get` injectable (CI mockée)."""

    API = "https://meet.googleapis.com/v2"

    def __init__(self, oauth, *, http_get: Callable[..., tuple[int, dict]] | None = None) -> None:
        self._oauth = oauth
        self._get = http_get or _requests_get

    def list_recordings(self, conference_record_id: str) -> tuple[list[dict], str]:
        """Liste TOUS les enregistrements du conferenceRecord (pagination `nextPageToken`).
        Lève `MeetRecordingError` sur statut HTTP non-200 (le reconciler retentera)."""
        token = self._oauth.token()
        base = f"{self.API}/conferenceRecords/{conference_record_id}/recordings"
        headers = {"Authorization": f"Bearer {token}"}
        recordings: list[dict] = []
        page_token = ""
        seen_tokens: set[str] = set()
        while True:
            url = f"{base}?pageToken={page_token}" if page_token else base
            status, body = self._get(url, headers=headers)
            if status != 200:
                raise MeetRecordingError(
                    f"Meet API statut {status} sur conferenceRecords/"
                    f"{conference_record_id}/recordings")
            recordings.extend(r for r in (body.get("recordings") or []) if isinstance(r, dict))
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                return recordings, token
            if page_token in seen_tokens:       # garde-fou : token répété = pagination cassée
                raise MeetRecordingError("pagination Meet en boucle (nextPageToken répété)")
            seen_tokens.add(page_token)


class MeetArtifactProvider:
    """`ArtifactProvider` Meet pour le reconciler (Meet = POLL, pas de webhook). L'artefact
    porte le jeton Google (`auth_token`) → le fetcher Drive télécharge via Bearer."""

    capabilities = ProviderCapabilities(post_meeting_recording=True, post_meeting_transcript=True)

    def __init__(self, client: MeetApiClient) -> None:
        self._client = client
        self._adapter = MeetRecordingAdapter()

    async def fetch_artifacts(self, occurrence: ExternalMeetingOccurrence) -> list[RemoteArtifact]:
        recordings, token = await asyncio.get_event_loop().run_in_executor(
            None, self._client.list_recordings, occurrence.external_occurrence_id)
        artifacts: list[RemoteArtifact] = []
        for resource in recordings:
            try:
                rec = MeetRecording.from_recording(resource)
            except MeetRecordingError:
                continue                       # non finalisé / sans Drive → ignoré
            artifacts.append(replace(self._adapter.to_artifact(rec), auth_token=token))
        return artifacts

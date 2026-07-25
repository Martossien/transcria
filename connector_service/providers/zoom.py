"""Adaptateur Zoom post-réunion — Cloud Recording (A2, ADR-001 D8).

Zoom POSTe un webhook `recording.completed` (avec un `download_token` Bearer, valable
24 h) listant les `recording_files`. On sélectionne la piste AUDIO
(`recording_type == "audio_only"`, `file_type == "M4A"`) et on la traduit en occurrence +
artefact. Le téléchargement se fait par HTTP (`download_url` + `download_token`), pas MinIO
— d'où un fetcher distinct (plomberie ultérieure).

⚠️ Forme du payload d'après la doc Zoom (developers.zoom.us) ; à confirmer contre un compte
réel au gate manuel. Parsing tolérant en conséquence.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from connector_service.contract import (
    ExternalMeetingOccurrence,
    ProviderCapabilities,
    RemoteArtifact,
)

PROVIDER = "zoom"


class ZoomEventError(ValueError):
    """Webhook Zoom invalide ou sans piste audio exploitable."""


@dataclass(frozen=True)
class ZoomRecording:
    account_id: str
    meeting_uuid: str            # occurrence STABLE (unique/occurrence, ≠ meeting id réutilisé)
    host_id: str
    topic: str
    start_time: str
    download_token: str
    file_id: str
    download_url: str
    file_extension: str

    @classmethod
    def from_payload(cls, payload: dict) -> ZoomRecording:
        if not isinstance(payload, dict):
            raise ZoomEventError("payload Zoom invalide (objet attendu)")
        obj = ((payload.get("payload") or {}).get("object")) or {}
        uuid = str(obj.get("uuid") or "").strip()
        if not uuid:
            raise ZoomEventError("uuid de réunion Zoom manquant")
        audio = _pick_audio(obj.get("recording_files") or [])
        if audio is None:
            raise ZoomEventError("aucune piste audio exploitable dans recording_files")
        return cls(
            account_id=str((payload.get("payload") or {}).get("account_id") or ""),
            meeting_uuid=uuid,
            host_id=str(obj.get("host_id") or ""),
            topic=str(obj.get("topic") or ""),
            start_time=str(obj.get("start_time") or ""),
            download_token=str(payload.get("download_token") or ""),
            file_id=str(audio.get("id") or ""),
            download_url=str(audio.get("download_url") or ""),
            file_extension=str(audio.get("file_extension") or "M4A").lower(),
        )


def _pick_audio(files: list) -> dict | None:
    """Piste AUDIO d'abord (audio_only/M4A) ; sinon repli sur la 1re vidéo complète
    (ffmpeg en extraira l'audio). Ignore transcripts/chat."""
    completed = [f for f in files if isinstance(f, dict)
                 and str(f.get("status") or "completed").lower() == "completed"
                 and f.get("download_url")]
    for f in completed:
        if str(f.get("recording_type") or "").lower() == "audio_only" \
                or str(f.get("file_type") or "").upper() == "M4A":
            return f
    for f in completed:
        if str(f.get("file_type") or "").upper() == "MP4":
            return f
    return None


class ZoomRecordingAdapter:
    def to_occurrence(self, rec: ZoomRecording) -> ExternalMeetingOccurrence:
        return ExternalMeetingOccurrence(
            provider=PROVIDER,
            provider_account_id=rec.host_id or rec.account_id,
            external_occurrence_id=rec.meeting_uuid,
            organizer=rec.host_id,
            start_time=rec.start_time,
        )

    def to_artifact(self, rec: ZoomRecording) -> RemoteArtifact:
        return RemoteArtifact(
            artifact_id=rec.file_id,
            storage_uri=rec.download_url,          # HTTPS + download_token (pas s3://)
            media_type="audio/mp4" if rec.file_extension == "m4a" else "video/mp4",
            artifact_type="recording",
            auth_token=rec.download_token,         # jeton éphémère porté par l'événement
        )

    def dedup_key(self, rec: ZoomRecording) -> str:
        # occurrence = meeting_uuid (unique/occurrence) ; artefact = file_id. Jamais le
        # meeting id numérique seul (réutilisé par les réunions récurrentes).
        return "|".join((PROVIDER, rec.host_id or rec.account_id, rec.meeting_uuid, rec.file_id))


def _requests_get(url: str, *, headers: dict) -> tuple[int, dict]:
    import requests  # dép TranscrIA

    resp = requests.get(url, headers=headers, timeout=30)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


class ZoomApiClient:
    """« Get Meeting Recordings » — repli de réconciliation quand le download_token du
    webhook a expiré (>24 h) ou qu'un webhook a été manqué (ADR-001 D2-bis). `oauth` =
    ZoomOAuth ; `http_get` injectable (CI mockée)."""

    API = "https://api.zoom.us/v2"

    def __init__(self, oauth, *, http_get: Callable[..., tuple[int, dict]] | None = None) -> None:
        self._oauth = oauth
        self._get = http_get or _requests_get

    def get_recordings(self, meeting_uuid: str) -> tuple[dict, str]:
        """Retourne (payload recordings, access_token). L'UUID est double-encodé s'il
        contient `/` ou commence par `/` (exigence Zoom)."""
        token = self._oauth.token()
        uid = meeting_uuid
        if uid.startswith("/") or "//" in uid:
            uid = quote(quote(uid, safe=""), safe="")
        _status, body = self._get(f"{self.API}/meetings/{uid}/recordings",
                                  headers={"Authorization": f"Bearer {token}"})
        return body, token


class ZoomArtifactProvider:
    """`ArtifactProvider` Zoom pour le reconciler (poll). L'artefact porte le jeton OAuth
    (`auth_token`) → le fetcher HTTP télécharge via Bearer, pas via le download_token."""

    capabilities = ProviderCapabilities(post_meeting_recording=True, post_meeting_transcript=True)

    def __init__(self, client: ZoomApiClient) -> None:
        self._client = client
        self._adapter = ZoomRecordingAdapter()

    async def fetch_artifacts(self, occurrence: ExternalMeetingOccurrence) -> list[RemoteArtifact]:
        body, token = await asyncio.get_event_loop().run_in_executor(
            None, self._client.get_recordings, occurrence.external_occurrence_id)
        audio = _pick_audio(body.get("recording_files") or [])
        if audio is None:
            return []
        return [RemoteArtifact(
            artifact_id=str(audio.get("id") or ""),
            storage_uri=str(audio.get("download_url") or ""),
            media_type="audio/mp4",
            artifact_type="recording",
            auth_token=token,          # OAuth Bearer (pas le download_token du webhook)
        )]

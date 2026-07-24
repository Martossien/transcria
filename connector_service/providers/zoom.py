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

from dataclasses import dataclass

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact

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

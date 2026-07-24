"""Handler d'ingestion post-réunion GÉNÉRIQUE (A1-A4) + parsing par plateforme.

Un seul flux : payload plateforme → `(occurrence, artefact, dedup_key)` (via l'adaptateur
de la plateforme) → fetch de l'audio → `POST /v1/audio/ingest` (dedup_key en Idempotency-Key,
un rejeu ne double pas). Seule la fonction `parse` change d'une plateforme à l'autre.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from connector_service.bridge import IngestResult, JobsApiBridge
from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact
from connector_service.providers import meet as meet_provider
from connector_service.providers import teams as teams_provider
from connector_service.providers import visio as visio_provider
from connector_service.providers import zoom as zoom_provider


class ParsedArtifact(NamedTuple):
    occurrence: ExternalMeetingOccurrence
    artifact: RemoteArtifact
    dedup_key: str


def parse_visio(payload: dict) -> ParsedArtifact:
    task = visio_provider.VisioTask.from_payload(payload)
    a = visio_provider.VisioTaskAdapter(bucket=str(payload.get("_bucket") or "recordings"))
    return ParsedArtifact(a.to_occurrence(task), a.to_artifact(task), a.dedup_key(task))


def parse_zoom(payload: dict) -> ParsedArtifact:
    rec = zoom_provider.ZoomRecording.from_payload(payload)
    a = zoom_provider.ZoomRecordingAdapter()
    return ParsedArtifact(a.to_occurrence(rec), a.to_artifact(rec), a.dedup_key(rec))


def parse_teams(payload: dict) -> ParsedArtifact:
    rec = teams_provider.TeamsRecording.from_notification(payload)
    a = teams_provider.TeamsRecordingAdapter()
    return ParsedArtifact(a.to_occurrence(rec), a.to_artifact(rec), a.dedup_key(rec))


def parse_meet(payload: dict) -> ParsedArtifact:
    rec = meet_provider.MeetRecording.from_recording(payload)
    a = meet_provider.MeetRecordingAdapter()
    return ParsedArtifact(a.to_occurrence(rec), a.to_artifact(rec), a.dedup_key(rec))


PARSERS: dict[str, Callable[[dict], ParsedArtifact]] = {
    "visio": parse_visio, "zoom": parse_zoom, "teams": parse_teams, "meet": parse_meet,
}


class PostMeetingIngestHandler:
    """Générique : parse (plateforme) → fetch → ingest idempotent."""

    def __init__(self, provider: str, parse, fetcher, bridge: JobsApiBridge) -> None:
        self._provider = provider
        self._parse = parse
        self._fetcher = fetcher
        self._bridge = bridge

    async def handle(self, payload: dict) -> IngestResult:
        parsed = self._parse(payload)      # lève l'erreur de parsing propre à la plateforme
        audio, filename = await self._fetcher.fetch(parsed.artifact)
        return await self._bridge.ingest_recording(
            audio, filename,
            idempotency_key=parsed.dedup_key,
            provider=self._provider,
            external_meeting_id=parsed.occurrence.external_occurrence_id,
        )

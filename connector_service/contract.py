"""Contrat providers PAR CAPACITÉS (A0 — ADR-001 D3/D4).

Pur : dataclasses + Protocols, **zéro dépendance à transcria** (le connecteur parle à
TranscrIA par HTTP). Chaque plateforme n'implémente que les interfaces qu'elle supporte
et déclare ses `ProviderCapabilities` — un provider Teams-post ne porte JAMAIS un
`stream_audio()` factice. Les méthodes sont `async` (le service est I/O-bound).

Séparation plan de contrôle / plan de données (D4) :
- `ControlEvent` = petits messages DURABLES (enveloppe versionnée), bus fiable ;
- `AudioFrame` = flux LIVE lourd, jamais dans le bus durable (session média).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderCapabilities:
    """Manifeste : ce qu'une plateforme sait faire. Sert à désactiver proprement les
    opérations impossibles au lieu de lever `NotImplementedError`."""

    post_meeting_recording: bool = False
    post_meeting_transcript: bool = False
    live_audio: bool = False
    live_transcript: bool = False
    participant_identity: bool = False
    separate_tracks: bool = False


@dataclass(frozen=True)
class ExternalMeetingOccurrence:
    """Une occurrence de réunion côté plateforme (identité stable pour l'idempotence)."""

    provider: str
    provider_account_id: str
    external_occurrence_id: str
    organizer: str | None = None
    start_time: str | None = None      # ISO 8601 UTC
    end_time: str | None = None


@dataclass(frozen=True)
class RemoteArtifact:
    """Un artefact post-réunion récupérable (enregistrement / transcript / …)."""

    artifact_id: str
    storage_uri: str
    media_type: str
    artifact_type: str                 # "recording" | "transcript" | "chat" | …
    artifact_variant: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    participant_id: str | None = None


@dataclass(frozen=True)
class ExternalParticipant:
    participant_id: str
    display_name: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class RemoteTranscript:
    """Transcript fourni par la plateforme = artefact AUXILIAIRE (ADR-001 D7), jamais
    canonique par défaut."""

    transcript_id: str
    storage_uri: str
    language: str | None = None


@dataclass(frozen=True)
class ControlEvent:
    """Plan de contrôle : message durable. `deduplication_key` alimente l'idempotence."""

    event_id: str
    schema_version: int
    provider: str
    provider_account_id: str
    external_occurrence_id: str
    event_type: str
    occurred_at: str                   # ISO 8601 UTC
    received_at: str                   # ISO 8601 UTC
    correlation_id: str | None = None
    deduplication_key: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AudioFrame:
    """Plan de données : trame audio live. Timestamps désambiguïsés —
    `media_timestamp_ms` = position DANS la réunion ; `wall_clock_timestamp` = UTC."""

    provider: str
    provider_account_id: str
    external_occurrence_id: str
    track_id: str
    sequence_number: int               # par piste et par session
    media_timestamp_ms: int
    wall_clock_timestamp: str          # ISO 8601 UTC
    duration_ms: int
    encoding: str                      # ex. "pcm_s16le"
    sample_rate_hz: int
    channels: int
    sample_count: int
    payload: bytes
    participant_id: str | None = None
    participant_display_name: str | None = None


# Interfaces PAR CAPACITÉS — méthode-seule (isinstance runtime_checkable fiable).
# Convention : tout provider concret expose aussi un attribut `capabilities`
# (ProviderCapabilities) qui déclare ce qu'il supporte ; c'est lui qui pilote quelles
# interfaces sont appelées, pas un `NotImplementedError`.


@runtime_checkable
class ArtifactProvider(Protocol):
    """Récupère les artefacts post-réunion (webhook/OAuth/fetch en amont)."""

    async def fetch_artifacts(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[RemoteArtifact]: ...


@runtime_checkable
class ParticipantProvider(Protocol):
    async def fetch_participants(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[ExternalParticipant]: ...


@runtime_checkable
class PlatformTranscriptProvider(Protocol):
    async def fetch_platform_transcripts(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[RemoteTranscript]: ...


@runtime_checkable
class LiveMediaProvider(Protocol):
    def stream_audio(
        self, occurrence: ExternalMeetingOccurrence
    ) -> AsyncIterator[AudioFrame]: ...

"""Providers FACTICES en plusieurs combinaisons de capacités (A0 — DoD).

Prouvent que le contrat par capacités tient sans plateforme réelle : un provider
post-réunion n'a pas de `stream_audio` ; un provider live n'a pas de `fetch_artifacts`.
Servent de référence aux tests de contrat (communs + par capacité).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from connector_service.contract import (
    AudioFrame,
    ExternalMeetingOccurrence,
    ExternalParticipant,
    ProviderCapabilities,
    RemoteArtifact,
    RemoteTranscript,
)


class FakePostMeetingProvider:
    """Post-réunion uniquement (artefacts + participants + transcript plateforme).

    N'implémente PAS de flux live — sa capacité `live_audio` est False.
    """

    capabilities = ProviderCapabilities(
        post_meeting_recording=True,
        post_meeting_transcript=True,
        participant_identity=True,
    )

    async def fetch_artifacts(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[RemoteArtifact]:
        return [
            RemoteArtifact(
                artifact_id=f"{occurrence.external_occurrence_id}-rec",
                storage_uri="s3://bucket/rec.mp4",
                media_type="video/mp4",
                artifact_type="recording",
                sha256="0" * 64,
            )
        ]

    async def fetch_participants(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[ExternalParticipant]:
        return [ExternalParticipant(participant_id="p1", display_name="Alice")]

    async def fetch_platform_transcripts(
        self, occurrence: ExternalMeetingOccurrence
    ) -> list[RemoteTranscript]:
        return [
            RemoteTranscript(
                transcript_id=f"{occurrence.external_occurrence_id}-vtt",
                storage_uri="s3://bucket/t.vtt",
                language="fr",
            )
        ]


class FakeLiveProvider:
    """Live uniquement (pistes séparées par participant). Pas de post-réunion."""

    capabilities = ProviderCapabilities(
        live_audio=True,
        live_transcript=False,
        participant_identity=True,
        separate_tracks=True,
    )

    async def stream_audio(
        self, occurrence: ExternalMeetingOccurrence, frames: int = 3
    ) -> AsyncIterator[AudioFrame]:
        for seq in range(frames):
            yield AudioFrame(
                provider=occurrence.provider,
                provider_account_id=occurrence.provider_account_id,
                external_occurrence_id=occurrence.external_occurrence_id,
                track_id="track-p1",
                sequence_number=seq,
                media_timestamp_ms=seq * 20,
                wall_clock_timestamp="2026-07-24T20:00:00Z",
                duration_ms=20,
                encoding="pcm_s16le",
                sample_rate_hz=16000,
                channels=1,
                sample_count=320,
                payload=b"\x00" * 640,
                participant_id="p1",
                participant_display_name="Alice",
            )


class FakeFullProvider(FakePostMeetingProvider):
    """Toutes capacités (post-réunion + live) — hérite du post-réunion, ajoute le live."""

    capabilities = ProviderCapabilities(
        post_meeting_recording=True,
        post_meeting_transcript=True,
        live_audio=True,
        live_transcript=True,
        participant_identity=True,
        separate_tracks=True,
    )

    stream_audio = FakeLiveProvider.stream_audio

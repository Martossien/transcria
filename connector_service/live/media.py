"""Fournisseurs de MÉDIA LIVE (L1/L2/R1/C1) — `LiveMediaProvider` génériques.

Visio (LiveKit), Zoom (RTMS), Meet (Media API) et Teams (RTM) ne diffèrent QUE par leur
source de frames audio ; tous produisent du PCM par participant. `LiveAudioProvider`
convertit un flux de `RawFrame` (transport plateforme INJECTÉ) en `AudioFrame` normalisés
du contrat commun. Le transport réel (SDK livekit rtc, WebSocket RTMS, Meet Media API) est
un adaptateur derrière `frame_source`, confirmé au gate manuel — le cœur est testable.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NamedTuple, Protocol

from connector_service.contract import (
    AudioFrame,
    ExternalMeetingOccurrence,
    ProviderCapabilities,
)


class RawFrame(NamedTuple):
    participant_id: str
    payload: bytes
    sequence_number: int
    media_timestamp_ms: int
    wall_clock_timestamp: str          # ISO 8601 UTC (fourni par le transport)
    participant_name: str = ""
    track_id: str = ""
    # sample_rate_hz DOIT refléter le débit réellement livré par le transport, PAS une
    # constante universelle : Zoom RTMS et LiveKit négocient 16 kHz (défaut), mais Meet
    # Media livre ~48 kHz — le frame_source Meet fixe 48000 explicitement (rééch. en aval).
    sample_rate_hz: int = 16000
    channels: int = 1
    encoding: str = "pcm_s16le"


class FrameSource(Protocol):
    """Transport d'une plateforme : yield les frames PCM par participant d'une occurrence."""

    def frames(self, occurrence: ExternalMeetingOccurrence) -> AsyncIterator[RawFrame]: ...


# Capacités live par plateforme (séparation des pistes = identité par participant).
_LIVE_CAPS = ProviderCapabilities(live_audio=True, live_transcript=False,
                                  participant_identity=True, separate_tracks=True)


class LiveAudioProvider:
    """`LiveMediaProvider` générique. `provider` ∈ {visio, zoom, meet, teams}."""

    def __init__(self, provider: str, frame_source: FrameSource, *,
                 capabilities: ProviderCapabilities = _LIVE_CAPS) -> None:
        self._provider = provider
        self._source = frame_source
        self.capabilities = capabilities

    async def stream_audio(self, occurrence: ExternalMeetingOccurrence) -> AsyncIterator[AudioFrame]:
        async for rf in self._source.frames(occurrence):
            yield AudioFrame(
                provider=self._provider,
                provider_account_id=occurrence.provider_account_id,
                external_occurrence_id=occurrence.external_occurrence_id,
                track_id=rf.track_id or f"track-{rf.participant_id}",
                sequence_number=rf.sequence_number,
                media_timestamp_ms=rf.media_timestamp_ms,
                wall_clock_timestamp=rf.wall_clock_timestamp,
                duration_ms=_duration_ms(rf),
                encoding=rf.encoding,
                sample_rate_hz=rf.sample_rate_hz,
                channels=rf.channels,
                sample_count=_sample_count(rf),
                payload=rf.payload,
                participant_id=rf.participant_id,
                participant_display_name=rf.participant_name or None,
            )


def _sample_count(rf: RawFrame) -> int:
    # PCM 16-bit : 2 octets/échantillon/canal.
    if rf.encoding == "pcm_s16le" and rf.channels:
        return len(rf.payload) // (2 * rf.channels)
    return 0


def _duration_ms(rf: RawFrame) -> int:
    samples = _sample_count(rf)
    return int(samples * 1000 / rf.sample_rate_hz) if rf.sample_rate_hz else 0


def visio_live_provider(frame_source: FrameSource) -> LiveAudioProvider:
    """Visio/LiveKit — adapter le worker `multi_user_transcriber.py` (STT pluggable → nous)."""
    return LiveAudioProvider("visio", frame_source)


def zoom_rtms_provider(frame_source: FrameSource) -> LiveAudioProvider:
    """Zoom RTMS — PCM L16 16 k par participant (`data_opt=2`), WebSocket."""
    return LiveAudioProvider("zoom", frame_source)


def meet_media_provider(frame_source: FrameSource) -> LiveAudioProvider:
    """Meet Media API (Developer Preview, R1) — WebRTC *receive-only*, ~48 kHz.

    ⚠ Couverture PLAFONNÉE : Meet ne démultiplexe que les **3 flux les plus forts**
    (loudest speakers), identifiés par CSRC → participant. `participant_identity` reste
    vrai pour ces flux, mais `separate_tracks` ne garantit PAS une piste par participant
    au-delà de 3 locuteurs simultanés. Le frame_source Meet fixe `sample_rate_hz=48000`.
    """
    return LiveAudioProvider("meet", frame_source)


def teams_rtm_provider(frame_source: FrameSource) -> LiveAudioProvider:
    """Teams RTM (C1, dernier recours — MS déconseille)."""
    return LiveAudioProvider("teams", frame_source)

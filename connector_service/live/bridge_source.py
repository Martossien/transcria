"""Pont PCM NEUTRE — un acquéreur média externe pousse l'audio par participant à TranscrIA.

Certaines plateformes n'ont pas de client média officiel en Python : Teams temps réel repose
sur la plateforme `Microsoft.Skype.Bots.Media` (.NET/Windows), et un bot navigateur capte
dans une page. Plutôt que de réécrire ces piles en Python (impraticable), on définit **un
contrat neutre** : l'acquéreur (sidecar .NET, bot, futur SDK natif) envoie des messages JSON
PCM-par-participant, et cette `FrameSource` les convertit en `RawFrame` — comme
`RtmsMediaFrameSource` le fait pour le flux média Zoom.

Contrat d'un message (acquéreur → TranscrIA) :
    {"participant_id": str, "participant_name": str (optionnel),
     "pcm": "<base64 PCM s16le>" (ou bytes), "sample_rate_hz": int, "channels": int,
     "media_timestamp_ms": int (optionnel)}
La séquence est synthétisée par participant ; le wall-clock = heure d'arrivée (injectable).
"""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import RawFrame


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bridge_message(msg: object) -> tuple[str, bytes, int, int, str, int] | None:
    """Message du pont → (participant_id, payload, sample_rate_hz, channels, name, media_ts).
    Retourne None si le message n'est pas une frame PCM exploitable."""
    if not isinstance(msg, dict):
        return None
    pcm = msg.get("pcm")
    if pcm is None:
        return None
    payload = base64.b64decode(pcm) if isinstance(pcm, str) else bytes(pcm)
    if not payload:
        return None
    return (
        str(msg.get("participant_id") or ""),
        payload,
        int(msg.get("sample_rate_hz") or 16000),
        int(msg.get("channels") or 1),
        str(msg.get("participant_name") or ""),
        int(msg.get("media_timestamp_ms") or 0),
    )


# open(occurrence) -> AsyncIterator[dict] : messages du sidecar/bot déjà décodés (JSON).
BridgeMessages = Callable[[ExternalMeetingOccurrence], AsyncIterator[dict]]


class MediaBridgeFrameSource:
    """`FrameSource` neutre : convertit le flux de messages PCM d'un acquéreur externe en
    `RawFrame` par participant (séquence synthétisée/participant, wall-clock = arrivée). Le
    transport réel (serveur WS qui reçoit le sidecar) est injecté → cœur testable."""

    def __init__(self, messages: BridgeMessages, *, now: Callable[[], str] | None = None) -> None:
        self._open = messages
        self._now = now or _utc_now_iso

    async def frames(self, occurrence: ExternalMeetingOccurrence) -> AsyncIterator[RawFrame]:
        seq: dict[str, int] = {}
        async for msg in self._open(occurrence):
            parsed = parse_bridge_message(msg)
            if parsed is None:
                continue
            pid, payload, rate, channels, name, media_ts = parsed
            n = seq[pid] = seq.get(pid, -1) + 1
            yield RawFrame(
                participant_id=pid,
                payload=payload,
                sequence_number=n,
                media_timestamp_ms=media_ts,
                wall_clock_timestamp=self._now(),
                participant_name=name,
                track_id=f"track-{pid}",
                sample_rate_hz=rate,
                channels=channels,
                encoding="pcm_s16le",
            )

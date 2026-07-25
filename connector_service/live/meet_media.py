"""Transport Google Meet Media API (R1, Developer Preview) — audio LIVE WebRTC receive-only.

⚠️ RÉALITÉ (audit vs meet-media-api-samples) : ce n'est PAS du gRPC/HTTP mais du **WebRTC**
(client aiortc / C++), on rejoint via `spaces/{space}:connectActiveConference` (v2beta), l'audio
arrive à **~48 kHz**, l'admission dans la réunion est HUMAINE, et Meet ne démultiplexe que les
**3 flux les plus forts** (loudest speakers), identifiés par **CSRC** RTP → participant. La
couverture est donc plafonnée au-delà de 3 locuteurs simultanés.

Le CŒUR testable ici est la résolution CSRC→participant et le mapping vers `RawFrame` (via le
`DemuxFrameSource` commun, à 48 kHz). Le client WebRTC réel (offer/answer, jitter buffer,
décodage Opus→PCM, admission) est la glue injectée, confirmée au gate manuel.
"""
from __future__ import annotations

from connector_service.live._demux import DemuxedFrame, DemuxFrameSource

MEET_SAMPLE_RATE_HZ = 48000            # Meet Media livre ~48 kHz (≠ 16 k Zoom/LiveKit)
MEET_MAX_STREAMS = 3                   # plafond : 3 flux audio démuxés (loudest speakers)

# Le débit Meet diffère du défaut : le source Meet est un DemuxFrameSource nourri de frames 48 k.
MeetMediaFrameSource = DemuxFrameSource


def meet_demuxed_frame(csrc: int, payload: bytes,
                       participant_by_csrc: dict[int, tuple[str, str]], *,
                       sample_rate_hz: int = MEET_SAMPLE_RATE_HZ) -> DemuxedFrame:
    """Une contribution audio Meet (identifiée par son **CSRC** RTP) → `DemuxedFrame` 48 kHz.

    `participant_by_csrc` mappe un CSRC vers `(participant_id, display_name)` (fourni par les
    métadonnées de session Meet). Un CSRC non encore connu retombe sur un id `csrc-{n}`
    plutôt que de perdre la frame — la ré-association se fait quand les métadonnées arrivent.
    """
    pid, name = participant_by_csrc.get(csrc, (f"csrc-{csrc}", ""))
    return DemuxedFrame(participant_id=str(pid), payload=payload,
                        sample_rate_hz=sample_rate_hz, channels=1, participant_name=name)

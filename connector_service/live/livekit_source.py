"""Transport Visio/LiveKit (L1) — audio LIVE par participant via `rtc.AudioStream`.

`rtc.AudioFrame` (livekit-rtc) n'expose que 4 attributs — `data` (PCM s16le entrelacé),
`sample_rate`, `num_channels`, `samples_per_channel` — SANS identité, séquence ni horodatage.
L'identité vient du PARTICIPANT abonné (`participant.identity`), pas de la frame ; séquence et
horloge sont donc synthétisées (cf. `_demux`). Le débit natif LiveKit est 48 kHz : le transport
réel force 16 kHz/mono à la création du stream (`AudioStream.from_participant(..., sample_rate=
16000, num_channels=1)`, cf. echo-agent.py) — `LiveKitFrame` reflète ce qui est livré.

Le CŒUR (mapping → RawFrame) est le `DemuxFrameSource` commun ; l'établissement de la room,
l'abonnement micro et la fusion des `AudioStream` par participant sont la glue injectée,
confirmée au gate manuel.
"""
from __future__ import annotations

from connector_service.live._demux import DemuxedFrame, DemuxFrameSource

DEFAULT_IDENTITY = "transcria-bot"
TARGET_SAMPLE_RATE_HZ = 16000
TARGET_CHANNELS = 1

# LiveKit livre du 16 kHz/mono (forcé à la création du stream) : le défaut de DemuxedFrame convient.
LiveKitFrame = DemuxedFrame
LiveKitFrameSource = DemuxFrameSource


def livekit_access_token(api_key: str, api_secret: str, room: str, *,
                         identity: str = DEFAULT_IDENTITY, name: str = "") -> str:
    """Jeton d'accès LiveKit (`room_join` + `can_subscribe`) pour le bot transcripteur.
    Dépend de `livekit-api` (importé paresseusement) ; confirmé au gate manuel."""
    from livekit import api  # dép opt-in

    grants = api.VideoGrants(room_join=True, room=room, can_subscribe=True)
    token = api.AccessToken(api_key, api_secret).with_identity(identity).with_grants(grants)
    if name:
        token = token.with_name(name)
    return token.to_jwt()

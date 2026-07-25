"""Transport Microsoft Teams RTM (C1, DERNIER RECOURS) — audio LIVE via media platform.

⚠️ Microsoft DÉCONSEILLE explicitement les bots « real-time media » (RTM) : lourds (SDK
Communications calling, plateforme média historiquement Windows-centrée), fragiles, et la voie
OFFICIELLE recommandée reste le POST-réunion (`getAllRecordings`/`getAllTranscripts`, déjà
implémenté). Ce module n'existe que pour couvrir C1 du plan si un flux temps réel Teams devient
indispensable — à n'activer qu'en connaissance de cause.

Une fois l'audio décodé par piste, le mapping vers `RawFrame` est identique aux autres
transports WebRTC-like (`DemuxFrameSource` commun, 16 kHz). L'établissement de l'appel et le
décodage média sont la glue injectée, confirmée au gate manuel.
"""
from __future__ import annotations

from connector_service.live._demux import DemuxedFrame, DemuxFrameSource

TEAMS_RTM_SAMPLE_RATE_HZ = 16000

TeamsRtmFrame = DemuxedFrame
TeamsRtmFrameSource = DemuxFrameSource

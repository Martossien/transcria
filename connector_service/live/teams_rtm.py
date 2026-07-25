"""Transport Microsoft Teams RTM (C1, DERNIER RECOURS) — audio LIVE via media platform.

⚠️ Il n'existe PAS de voie *officielle* praticable en Python pour l'audio temps réel Teams :
la plateforme média des bots RTM Microsoft repose sur le **SDK Graph Communications .NET**
(historiquement Windows-centré), que Microsoft DÉCONSEILLE par ailleurs. La voie officielle
recommandée reste le POST-réunion (`getAllRecordings`/`getAllTranscripts`, déjà implémenté),
et la voie live praticable est le **bot navigateur**. Ce module ne fournit donc PAS de client
RTM .NET : il n'expose que le cœur de démux commun, prêt à recevoir du PCM par participant de
n'importe quelle source (y compris le bot). On ne fabrique pas de fausse glue .NET-en-Python.

Une fois l'audio décodé par piste, le mapping vers `RawFrame` est identique aux autres
transports WebRTC-like (`DemuxFrameSource` commun, 16 kHz).
"""
from __future__ import annotations

from connector_service.live._demux import DemuxedFrame, DemuxFrameSource

TEAMS_RTM_SAMPLE_RATE_HZ = 16000

TeamsRtmFrame = DemuxedFrame
TeamsRtmFrameSource = DemuxFrameSource

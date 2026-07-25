"""Transport Zoom RTMS (L2) — audio LIVE PCM par participant.

Flux réel (rtms-samples RTMS_CONNECTION_FLOW.md + SDK `rtms`) : deux WebSockets successifs
(signaling puis média), signature de handshake dédiée, keepalive bidirectionnel, puis les
paquets audio `msg_type:14`. On négocie **L16 / 16 kHz / mono / `data_opt=2`** (un flux par
participant) — c'est ce que la diarisation par piste exige, et c'est supérieur au MIXED des
samples.

Ce module fournit les briques PURES (builders de handshake, keepalive, parse audio) et un
`FrameSource` qui consomme le flux de messages du socket MÉDIA déjà établi (injecté →
testable en CI ; l'établissement réel des deux WS est la glue confirmée au gate manuel).

Format audio confirmé par le parseur officiel (`mediaSocketMessageHandler.js`) ET l'adaptateur
Attendee (lu, non copié) : tout est niché sous `content` —
``{"msg_type":14, "content":{"user_id":int, "user_name":str, "data":"<b64 PCM>", "timestamp":epoch_ms}}``.
La variante « simplifiée » de la doc (base64 direct sous `content`) est FAUSSE.
"""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live.media import RawFrame
from connector_service.signatures import rtms_handshake_signature

# --- Types de messages RTMS (signaling + média) --- #
SIGNALING_HANDSHAKE = 1
SIGNALING_HANDSHAKE_RESP = 2
MEDIA_HANDSHAKE = 3
MEDIA_HANDSHAKE_RESP = 4
CLIENT_READY = 7
KEEPALIVE_REQ = 12
KEEPALIVE_RESP = 13
DATA_AUDIO = 14

# --- Paramètres média audio (MEDIA_PARAMETERS.md) — L16/16k/mono/multi-flux --- #
AUDIO_MEDIA_PARAMS = {
    "content_type": 2,   # RAW_AUDIO
    "codec": 1,          # L16 (PCM 16-bit)
    "sample_rate": 1,    # 1 = 16 kHz (cible Whisper) ; 2 = 32 kHz
    "channel": 1,        # MONO (L16 ne supporte que mono ; stéréo = Opus)
    "data_opt": 2,       # AUDIO_MULTI_STREAMS : un flux par participant
    "send_rate": 20,
}


def signaling_handshake(client_id: str, client_secret: str, meeting_uuid: str,
                        rtms_stream_id: str, sequence: int) -> dict:
    """Message `msg_type:1` du WebSocket de signaling."""
    return {
        "msg_type": SIGNALING_HANDSHAKE,
        "protocol_version": 1,
        "meeting_uuid": meeting_uuid,
        "rtms_stream_id": rtms_stream_id,
        "sequence": sequence,
        "signature": rtms_handshake_signature(
            client_id, client_secret, meeting_uuid, rtms_stream_id),
        "buffer_data": False,
    }


def media_handshake(client_id: str, client_secret: str, meeting_uuid: str,
                    rtms_stream_id: str) -> dict:
    """Message `msg_type:3` du WebSocket média : négocie l'audio L16 16 k mono par
    participant, chiffrement de payload désactivé."""
    return {
        "msg_type": MEDIA_HANDSHAKE,
        "protocol_version": 1,
        "meeting_uuid": meeting_uuid,
        "rtms_stream_id": rtms_stream_id,
        "signature": rtms_handshake_signature(
            client_id, client_secret, meeting_uuid, rtms_stream_id),
        "media_type": 1,             # audio seul (32 = tout)
        "payload_encryption": False,
        "media_params": {"audio": dict(AUDIO_MEDIA_PARAMS)},
    }


def client_ready(rtms_stream_id: str) -> dict:
    """Message `msg_type:7` (sur le socket SIGNALING) : démarre l'envoi des données une
    fois le handshake média accepté."""
    return {"msg_type": CLIENT_READY, "rtms_stream_id": rtms_stream_id}


def keepalive_response(msg: object) -> dict | None:
    """Réponse `msg_type:13` à un keepalive `msg_type:12` (renvoie le MÊME timestamp).
    Vaut pour les DEUX sockets ; ne pas répondre = déconnexion. Retourne None sinon."""
    if isinstance(msg, dict) and msg.get("msg_type") == KEEPALIVE_REQ:
        return {"msg_type": KEEPALIVE_RESP, "timestamp": msg.get("timestamp")}
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_audio_frame(msg: object, *, sequence_number: int,
                      wall_clock_timestamp: str) -> RawFrame | None:
    """Paquet `msg_type:14` → RawFrame. Champs nichés sous `content`. Retourne None si le
    message n'est pas un paquet audio exploitable.

    RTMS ne fournit ni numéro de séquence ni horodatage mural : `sequence_number` est
    synthétisé par l'appelant (compteur/participant) et `wall_clock_timestamp` est l'heure
    d'ARRIVÉE (le `content.timestamp` epoch reste en `media_timestamp_ms` pour l'alignement,
    sa nature absolue/relative variant selon la plateforme). `content.user_id` (int) → str.
    ⚠️ Endianness L16 (`pcm_s16le`) à valider sur un flux réel au gate.
    """
    if not isinstance(msg, dict) or msg.get("msg_type") != DATA_AUDIO:
        return None
    content = msg.get("content")
    if not isinstance(content, dict) or not content.get("data"):
        return None
    return RawFrame(
        participant_id=str(content.get("user_id", "")),
        participant_name=str(content.get("user_name") or ""),
        payload=base64.b64decode(content["data"]),
        sequence_number=sequence_number,
        media_timestamp_ms=int(content.get("timestamp") or 0),
        wall_clock_timestamp=wall_clock_timestamp,
        sample_rate_hz=16000,
        channels=1,
        encoding="pcm_s16le",
    )


# open(occurrence) -> AsyncIterator[dict] : messages déjà décodés du socket MÉDIA établi.
MediaMessages = Callable[[ExternalMeetingOccurrence], AsyncIterator[dict]]


class RtmsMediaFrameSource:
    """`FrameSource` Zoom RTMS : transforme le flux de messages du socket média en RawFrame
    audio par participant. Répond aux keepalive média via `on_keepalive` (injecté). Le
    numéro de séquence est synthétisé par participant, le wall-clock dérivé du timestamp
    epoch du paquet (déterministe)."""

    def __init__(self, media_messages: MediaMessages, *,
                 on_keepalive: Callable[[dict], Awaitable[None]] | None = None,
                 now: Callable[[], str] | None = None) -> None:
        self._open = media_messages
        self._on_keepalive = on_keepalive
        self._now = now or _utc_now_iso

    async def frames(self, occurrence: ExternalMeetingOccurrence) -> AsyncIterator[RawFrame]:
        seq: dict[str, int] = {}
        async for msg in self._open(occurrence):
            ka = keepalive_response(msg)
            if ka is not None:
                if self._on_keepalive is not None:
                    await self._on_keepalive(ka)
                continue
            if not isinstance(msg, dict) or msg.get("msg_type") != DATA_AUDIO:
                continue                       # transcript(17)/vidéo(15)/chat(18)… ignorés
            content = msg.get("content")
            pid = str(content.get("user_id", "")) if isinstance(content, dict) else ""
            n = seq[pid] = seq.get(pid, -1) + 1
            frame = parse_audio_frame(msg, sequence_number=n,
                                      wall_clock_timestamp=self._now())
            if frame is not None:
                yield frame

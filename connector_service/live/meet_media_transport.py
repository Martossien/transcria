"""Vrai transport Google Meet Media API (R1, v2beta) — WebRTC receive-only, dep opt-in aiortc.

Contrat vérifié sur le sample officiel meet-media-api-samples. Deux morceaux :
- **Logique pure, testée en CI** : requête `connectActiveConference` (`{"offer": sdp}`), parse
  de la réponse (`answer` / `error.status`), registres des data channels `media-entries` et
  `participants`, résolution CSRC→participant (en sautant le CSRC magique 42 = locuteur le plus
  fort), état de session (`STATE_WAITING/JOINED/DISCONNECTED`), requête `leave`.
- **Squelette aiortc, gate manuel** : `RTCPeerConnection` avec 3 transceivers audio `recvonly`
  + 5 data channels, handshake offer→POST→answer→setRemoteDescription, décodage Opus→PCM 48 k.

⚠️ Points à valider au gate (non prouvables en CI) : (1) aiortc **n'expose pas nativement les
CSRC** RTP contributifs — il faudra les extraire au niveau RTP (patch/hook), sinon l'attribution
par locuteur retombe sur « inconnu » ; (2) **scopes OAuth exacts + type de jeton** (le sample
consomme un token pré-minté sans les documenter) ; (3) admission HUMAINE requise (l'initiateur
admet le client via l'UI Meet ; défaut 2 min).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live._demux import DemuxedFrame

MEET_API_BASE = "https://meet.googleapis.com/v2beta"
LOUDEST_SPEAKER_CSRC = 42               # CSRC magique : marque le locuteur le plus fort
AUDIO_STREAM_COUNT = 3                  # exactement 3 transceivers audio recvonly (ou aucun)
MEET_SAMPLE_RATE_HZ = 48000            # Opus décodé = 48 kHz
DATA_CHANNELS = ("media-entries", "media-stats", "participants",
                 "session-control", "video-assignment")

STATE_WAITING = "STATE_WAITING"
STATE_JOINED = "STATE_JOINED"
STATE_DISCONNECTED = "STATE_DISCONNECTED"


class MeetMediaError(RuntimeError):
    """Échec de `connectActiveConference` (status RPC Google) ou média Meet."""


# --------------------------------------------------------------------------- #
#  REST : connectActiveConference
# --------------------------------------------------------------------------- #
def connect_active_conference_request(space: str, sdp_offer: str, token: str
                                      ) -> tuple[str, dict, dict]:
    """(url, body, headers) du POST de jointure. Le SDP offer est une STRING sous `offer`."""
    url = f"{MEET_API_BASE}/spaces/{space}:connectActiveConference"
    body = {"offer": sdp_offer}
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json;charset=UTF-8"}
    return url, body, headers


def parse_connect_response(resp: dict) -> str:
    """Retourne le SDP answer, ou lève `MeetMediaError(status)` si la réponse porte `error`."""
    answer = resp.get("answer")
    if isinstance(answer, str) and answer:
        return answer
    status = (resp.get("error") or {}).get("status") or "UnknownError"
    raise MeetMediaError(f"connectActiveConference refusé: {status}")


# --------------------------------------------------------------------------- #
#  Data channels : media-entries + participants (attribution locuteur)
# --------------------------------------------------------------------------- #
def _display_name(participant: dict) -> str:
    for kind in ("signedInUser", "anonymousUser", "phoneUser"):
        sub = participant.get(kind)
        if isinstance(sub, dict) and sub.get("displayName"):
            return str(sub["displayName"])
    return ""


class MediaEntriesRegistry:
    """État du data channel `media-entries` : mappe un CSRC audio → mediaEntry (identité)."""

    def __init__(self) -> None:
        self._by_id: dict[Any, dict] = {}

    def apply(self, msg: dict) -> None:
        for res in msg.get("resources") or []:
            entry = res.get("mediaEntry")
            if isinstance(entry, dict) and "id" in res:
                self._by_id[res["id"]] = entry
        for dele in msg.get("deletedResources") or []:
            self._by_id.pop(dele.get("id"), None)

    def entry_for_csrc(self, csrc: int) -> dict | None:
        for entry in self._by_id.values():
            if entry.get("audioCsrc") == csrc:
                return entry
        return None


class ParticipantsRegistry:
    """État du data channel `participants` : mappe `participantKey` → nom affiché."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def apply(self, msg: dict) -> None:
        for res in msg.get("resources") or []:
            p = res.get("participant")
            if isinstance(p, dict):
                key = p.get("participantKey") or p.get("name")
                if key:
                    self._names[str(key)] = _display_name(p)

    def display_name(self, key: str) -> str:
        return self._names.get(key, "")


def pick_contributing_csrc(csrcs: list[int]) -> int | None:
    """Premier CSRC ≠ 42 (on saute le marqueur « locuteur le plus fort »). None si aucun."""
    for csrc in csrcs:
        if csrc != LOUDEST_SPEAKER_CSRC:
            return csrc
    return None


def meet_frame_from_rtp(csrcs: list[int], pcm: bytes, media_entries: MediaEntriesRegistry,
                        participants: ParticipantsRegistry, *,
                        sample_rate_hz: int = MEET_SAMPLE_RATE_HZ) -> DemuxedFrame:
    """Une frame audio Meet (CSRC RTP + PCM décodé) → `DemuxedFrame` 48 kHz, attribuée au
    participant via `media-entries` puis `participants`. CSRC inconnu → id `csrc-{n}`
    (frame conservée), en attendant l'arrivée des métadonnées."""
    csrc = pick_contributing_csrc(csrcs)
    pid, name = (f"csrc-{csrc}" if csrc is not None else "unknown"), ""
    if csrc is not None:
        entry = media_entries.entry_for_csrc(csrc)
        if entry is not None:
            key = str(entry.get("participantKey") or entry.get("participant") or pid)
            pid, name = key, participants.display_name(key)
    return DemuxedFrame(participant_id=pid, payload=pcm, sample_rate_hz=sample_rate_hz,
                        channels=1, participant_name=name)


# --------------------------------------------------------------------------- #
#  Data channel : session-control (admission)
# --------------------------------------------------------------------------- #
def connection_state(msg: dict) -> str | None:
    """État d'admission depuis un message `session-control` (`sessionStatus.connectionState`)."""
    for res in msg.get("resources") or []:
        state = (res.get("sessionStatus") or {}).get("connectionState")
        if state:
            return str(state)
    return None


def leave_request(request_id: int) -> dict:
    """Message `session-control` pour partir proprement (`leave` = objet vide)."""
    return {"request": {"requestId": request_id, "leave": {}}}


# --------------------------------------------------------------------------- #
#  Squelette aiortc réel (dep opt-in) — gate manuel
# --------------------------------------------------------------------------- #
def meet_media_demux_source(space: str, token: str, http_post: Callable[..., dict], *,
                            video_stream_count: int = 0) -> Callable[
                                [ExternalMeetingOccurrence], AsyncIterator[DemuxedFrame]]:
    """Source démuxée Meet Media réelle (dep opt-in `aiortc`). Établit la PeerConnection
    receive-only (3 audio recvonly + data channels), fait le handshake offer→POST→answer,
    et yield un `DemuxedFrame` 48 kHz par frame audio décodée, attribué via `media-entries`.
    NON testé en CI (aiortc + serveur Meet + admission humaine) → gate manuel.

    `http_post(url, json, headers) -> dict` est injecté (le POST OAuth réel). Voir les
    avertissements du module sur l'extraction CSRC dans aiortc."""
    def _factory(_occurrence: ExternalMeetingOccurrence) -> AsyncIterator[DemuxedFrame]:
        async def _open() -> AsyncIterator[DemuxedFrame]:
            from aiortc import RTCPeerConnection, RTCSessionDescription  # dép opt-in

            media_entries = MediaEntriesRegistry()
            participants = ParticipantsRegistry()
            pc = RTCPeerConnection()
            for _ in range(AUDIO_STREAM_COUNT):
                pc.addTransceiver("audio", direction="recvonly")
            for _ in range(video_stream_count):
                pc.addTransceiver("video", direction="recvonly")
            channels = {name: pc.createDataChannel(name, ordered=True) for name in DATA_CHANNELS}

            @channels["media-entries"].on("message")
            def _on_media_entries(raw: Any) -> None:
                import json
                media_entries.apply(json.loads(raw))

            @channels["participants"].on("message")
            def _on_participants(raw: Any) -> None:
                import json
                participants.apply(json.loads(raw))

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            url, body, headers = connect_active_conference_request(
                space, pc.localDescription.sdp, token)
            answer_sdp = parse_connect_response(http_post(url, body, headers))
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

            # NOTE gate : brancher ici pc.on("track") → recv() → extraire les CSRC RTP →
            # meet_frame_from_rtp(csrcs, pcm, media_entries, participants) → yield.
            # aiortc n'expose pas les CSRC nativement : hook RTP requis (cf. avertissement module).
            return
            yield  # pragma: no cover  (générateur : la vraie boucle média est câblée au gate)

        return _open()
    return _factory

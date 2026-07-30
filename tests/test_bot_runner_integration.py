"""Bot — intégration du runner : VRAI serveur de pont WebSocket + faux navigateur poussant
du PCM. Aucun navigateur réel : le driver factice joue son rôle (client WS qui se connecte,
pousse des frames, puis se déconnecte à la « fermeture du navigateur »).

C'est le test qui aurait attrapé la régression B1 (le handler du pont retournait aussitôt →
websockets fermait la connexion → aucun audio ne parvenait jamais à la session).
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket

import pytest

websockets = pytest.importorskip("websockets")   # dép opt-in du connecteur

from connector_service.bot.runner import run_bot_session  # noqa: E402
from connector_service.contract import ExternalMeetingOccurrence  # noqa: E402
from connector_service.live.agreement import Word  # noqa: E402
from connector_service.live.session import Hypothesis  # noqa: E402

OCC = ExternalMeetingOccurrence(provider="bot", provider_account_id="acct",
                                external_occurrence_id="salle-test")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pcm_message(participant: str) -> str:
    return json.dumps({"participant_id": participant,
                       "pcm": base64.b64encode(b"\x00\x00" * 160).decode("ascii"),
                       "sample_rate_hz": 16000, "channels": 1})


class FakeBrowserDriver:
    """`BrowserDriver` qui simule le navigateur : ouvre une VRAIE connexion WS vers le pont,
    pousse `frame_count` frames pendant la réunion, et coupe la connexion à `close()`."""

    def __init__(self, bridge_url: str, *, frame_count: int = 5, admitted: bool = True) -> None:
        self._bridge_url = bridge_url
        self._frame_count = frame_count
        self._admitted = admitted
        self._client = None
        self.sent = 0

    async def open(self, meeting_url: str) -> None:
        self._client = await websockets.connect(self._bridge_url)

    async def request_join(self, display_name: str) -> None:
        pass

    async def wait_admission(self, timeout_s: float) -> bool:
        return self._admitted

    async def run_until_ended(self) -> str:
        for _ in range(self._frame_count):
            await self._client.send(_pcm_message("alice"))
            self.sent += 1
            await asyncio.sleep(0)
        return "left_alone"

    async def leave(self) -> None:
        pass

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()      # « navigateur fermé » → fin du flux du pont


class _CountingTranscriber:
    """Transcripteur factice : compte les frames reçues, puis rend un tour final. C'est le
    patron du GATE réel (prouver que la capture arrive, avant de brancher un vrai STT)."""

    uses_local_agreement = False

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def stream(self, frames):
        async for frame in frames:
            self.seen.append(frame.participant_id)
        if self.seen:                       # un vrai STT n'émet rien sans audio
            yield Hypothesis(committed=[Word("bonjour", 0.0, 1.0)], is_final=True)


def _run(driver, transcriber, port, timeout=15):
    async def _main():
        return await asyncio.wait_for(
            run_bot_session("https://meet.jit.si/salle-test", OCC, driver, transcriber,
                            bridge_port=port), timeout)
    return asyncio.run(_main())


def test_runner_le_pont_reste_ouvert_et_le_pcm_arrive():
    """Régression B1 : le PCM poussé par le « navigateur » doit atteindre la session."""
    port = _free_port()
    transcriber = _CountingTranscriber()
    driver = FakeBrowserDriver(f"ws://127.0.0.1:{port}", frame_count=5)

    outcome, segments = _run(driver, transcriber, port)

    assert driver.sent == 5
    assert transcriber.seen == ["alice"] * 5     # ← 0 avec le bug (pont refermé aussitôt)
    assert outcome.admitted is True and outcome.reason == "left_alone"
    assert [s.text for s in segments] == ["bonjour"]
    assert segments[0].provenance == "final_live"


def test_runner_non_admis_se_termine_sans_pendre():
    """Jamais admis → aucune capture, pas de task fantôme, terminaison propre."""
    port = _free_port()
    transcriber = _CountingTranscriber()
    driver = FakeBrowserDriver(f"ws://127.0.0.1:{port}", frame_count=0, admitted=False)

    outcome, segments = _run(driver, transcriber, port)

    assert outcome.admitted is False and outcome.reason == "admission_timeout"
    assert transcriber.seen == [] and segments == []


def test_runner_erreur_navigateur_ne_pend_pas():
    """Un driver qui explose en pleine réunion : outcome `error`, pas de blocage."""
    port = _free_port()

    class BoomDriver(FakeBrowserDriver):
        async def run_until_ended(self):
            await self._client.send(_pcm_message("alice"))
            self.sent += 1
            raise RuntimeError("navigateur mort")

    transcriber = _CountingTranscriber()
    driver = BoomDriver(f"ws://127.0.0.1:{port}")

    outcome, _segments = _run(driver, transcriber, port)

    assert outcome.reason == "error"             # remonté proprement, pas de crash


class _NoticingDriver(FakeBrowserDriver):
    """Driver qui apprend l'adresse RÉELLE du pont (port auto-alloué)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bridge_url_received: str | None = None

    def set_bridge_url(self, bridge_url: str) -> None:
        self.bridge_url_received = bridge_url
        self._bridge_url = bridge_url


def test_port_du_pont_auto_alloue():
    """Un port FIXE ferait entrer en collision plusieurs bots sur la même machine."""
    transcriber = _CountingTranscriber()
    driver = _NoticingDriver("ws://127.0.0.1:0", frame_count=3)

    async def _main():
        return await asyncio.wait_for(
            run_bot_session("https://x/salle", OCC, driver, transcriber), 15)

    outcome, _ = asyncio.run(_main())
    assert driver.bridge_url_received is not None
    port = int(driver.bridge_url_received.rsplit(":", 1)[1])
    assert port > 0                                    # port réel attribué par le système
    assert outcome.admitted and transcriber.seen == ["alice"] * 3


def test_deux_bots_simultanes_ne_se_marchent_pas_dessus():
    """10 réunions = 10 bots : chacun doit avoir SON pont et SES frames."""
    async def _one(name: str, frames: int):
        transcriber = _CountingTranscriber()
        driver = _NoticingDriver("ws://127.0.0.1:0", frame_count=frames)
        outcome, segments = await run_bot_session(f"https://x/{name}", OCC, driver, transcriber)
        return driver.bridge_url_received, transcriber.seen, outcome

    async def _main():
        return await asyncio.wait_for(
            asyncio.gather(_one("salle-a", 4), _one("salle-b", 6)), 30)

    (url_a, seen_a, out_a), (url_b, seen_b, out_b) = asyncio.run(_main())
    assert url_a != url_b                              # ports DISTINCTS
    assert len(seen_a) == 4 and len(seen_b) == 6       # aucun mélange de flux
    assert out_a.admitted and out_b.admitted


def test_runner_on_final_recoit_chaque_tour_en_direct():
    """Vague 5, lot C : chaque tour FINAL atteint l'observateur `on_final` (le CLI y branche
    l'émission {"bot_caption": …}) — et un observateur défaillant ne casse jamais la session."""
    port = _free_port()
    transcriber = _CountingTranscriber()
    driver = FakeBrowserDriver(f"ws://127.0.0.1:{port}", frame_count=3)
    received: list = []

    def observer(seg):
        received.append(seg)
        raise RuntimeError("observateur cassé")     # ne doit rien casser

    async def _main():
        return await asyncio.wait_for(
            run_bot_session("https://meet.jit.si/salle-test", OCC, driver, transcriber,
                            bridge_port=port, on_final=observer), 15)
    outcome, segments = asyncio.run(_main())

    assert outcome.admitted is True
    assert [s.text for s in received] == ["bonjour"]
    assert segments == received                      # même flux : le direct n'invente rien


def test_emetteur_de_captions_json(capsys):
    """Le CLI émet une ligne {"bot_caption": …} par tour final (BOT_EVENTS=json)."""
    import json as _json

    from connector_service.bot.cli import _json_caption_emitter
    from connector_service.live.session import Segment

    _json_caption_emitter()(Segment("Bonjour à tous", 1.234, 2.5, "final_live", "Alice"))
    payload = _json.loads(capsys.readouterr().out.strip())
    assert payload["bot_caption"] == {"start": 1.234, "end": 2.5,
                                      "speaker": "Alice", "text": "Bonjour à tous"}

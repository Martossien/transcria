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

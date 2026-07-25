"""L1 — AudioFanIn : fusion des flux audio par participant (logique testable du transport)."""
from __future__ import annotations

import asyncio

from connector_service.live._demux import DemuxedFrame
from connector_service.live.livekit_transport import AudioFanIn


def _pcm_frame(pid, n):
    # objet « event » factice à la forme livekit (event.frame.data/...).
    class _Frame:
        data = b"\x00\x00" * 80
        sample_rate = 16000
        num_channels = 1

    class _Event:
        frame = _Frame()

    return _Event()


def _to_frame(pid):
    return lambda _ev: DemuxedFrame(participant_id=pid, payload=b"\x00\x00" * 80)


async def _finite(pid, count):
    for i in range(count):
        yield _pcm_frame(pid, i)
        await asyncio.sleep(0)


def test_fan_in_un_flux_ordonne():
    async def scenario():
        fan = AudioFanIn()
        fan.add_stream(_finite("p1", 3), _to_frame("p1"))
        await fan.wait_producers()          # producteurs finis AVANT la sentinelle → déterministe
        fan.stop()
        return [f async for f in fan.frames()]

    out = asyncio.run(scenario())
    assert len(out) == 3 and all(f.participant_id == "p1" for f in out)


def test_fan_in_fusionne_plusieurs_participants():
    async def scenario():
        fan = AudioFanIn()
        fan.add_stream(_finite("p1", 2), _to_frame("p1"))
        fan.add_stream(_finite("p2", 3), _to_frame("p2"))
        await fan.wait_producers()
        fan.stop()
        return [f async for f in fan.frames()]

    out = asyncio.run(scenario())
    counts = {}
    for f in out:
        counts[f.participant_id] = counts.get(f.participant_id, 0) + 1
    assert counts == {"p1": 2, "p2": 3}     # tous les items des deux flux, fusionnés


def test_fan_in_flux_defaillant_n_arrete_pas_les_autres():
    async def _boom(pid):
        yield _pcm_frame(pid, 0)
        raise RuntimeError("flux mort")      # ne doit PAS tuer la session

    async def scenario():
        fan = AudioFanIn()
        fan.add_stream(_boom("bad"), _to_frame("bad"))
        fan.add_stream(_finite("good", 2), _to_frame("good"))
        await fan.wait_producers()
        fan.stop()
        return [f async for f in fan.frames()]

    out = asyncio.run(scenario())
    counts = {}
    for f in out:
        counts[f.participant_id] = counts.get(f.participant_id, 0) + 1
    assert counts["good"] == 2 and counts.get("bad", 0) == 1   # l'item avant le crash passe


def test_fan_in_stop_termine_frames():
    async def scenario():
        fan = AudioFanIn()
        fan.stop()                           # aucune donnée → frames() se termine tout de suite
        return [f async for f in fan.frames()]

    assert asyncio.run(scenario()) == []

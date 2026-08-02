"""Bot Visio (lot V1, docs/VISIO_ZOOM_RUNNER.md) — sans LiveKit ni réseau.

Ce que ces tests verrouillent : le parse de salle suit la normalisation du BACKEND OFFICIEL
Visio (slug de l'URL — référence ~/reference/meet) ; la garde d'inactivité clôt une salle
désertée SANS jamais couper une réunion vivante ; le câblage complet (frames par
participant → tee pistes → segments → ingestion) tourne avec des fabriques factices et
rend les codes de sortie du contrat commun.
"""
from __future__ import annotations

import asyncio

import pytest

from connector_service.bot.visio import monitored_frames, parse_visio_room


class TestParseVisioRoom:
    def test_url_de_salle_slug(self):
        assert parse_visio_room("https://visio.exemple/abc-def-ghi") == "abc-def-ghi"
        assert parse_visio_room("https://visio.exemple/abc-def-ghi?x=1") == "abc-def-ghi"

    def test_nom_brut_normalise_comme_le_backend(self):
        # Même effet que slugify() Django (viewsets.py:271 du dépôt officiel).
        assert parse_visio_room("Réunion Été 2026") == "reunion-ete-2026"
        assert parse_visio_room("ma-salle") == "ma-salle"

    def test_entrees_invalides_refusees(self):
        for raw in ("", "   ", "https://visio.exemple/", "!!!"):
            with pytest.raises(ValueError):
                parse_visio_room(raw)


def _factory(frames, *, hang_after: bool = False):
    def make(_occurrence):
        async def gen():
            for frame in frames:
                yield frame
            if hang_after:                        # plus jamais de frame — salle désertée
                await asyncio.sleep(3600)
        return gen()
    return make


class TestMonitoredFrames:
    def test_premiere_frame_signale_in_meeting(self):
        seen = []
        wrapped = monitored_frames(_factory(["f1", "f2"]), on_first=lambda: seen.append(1),
                                   idle_timeout_s=5.0)

        async def collect():
            return [f async for f in wrapped(None)]
        assert asyncio.run(collect()) == ["f1", "f2"]
        assert seen == [1]                        # UNE fois, pas à chaque frame

    def test_salle_desertee_clot_le_flux(self):
        wrapped = monitored_frames(_factory(["f1"], hang_after=True),
                                   idle_timeout_s=0.05)

        async def collect():
            return [f async for f in wrapped(None)]
        assert asyncio.run(collect()) == ["f1"]   # la garde rend la main, pas d'attente infinie


class TestRunWiring:
    """Câblage de `run()` avec jeton et source factices — codes de sortie du contrat."""

    def _args(self, tmp_path, ref="https://visio.exemple/salle-test"):
        from connector_service.bot.visio import build_parser
        return build_parser().parse_args([ref, "--livekit-url", "wss://lk.exemple",
                                          "--idle-timeout-s", "0.2"])

    def _patch_transport(self, monkeypatch, frames):
        from connector_service.live._demux import DemuxedFrame
        payloads = [DemuxedFrame(participant_id=pid, payload=b"\x01\x02" * 320,
                                 sample_rate_hz=16000, participant_name=name)
                    for pid, name in frames]
        monkeypatch.setattr("connector_service.bot.visio.livekit_access_token",
                            lambda *a, **k: "jeton-factice")
        monkeypatch.setattr("connector_service.bot.visio.livekit_demux_source",
                            lambda url, token: _factory(payloads))

    def test_reunion_captee_code_0_et_ingestion(self, monkeypatch, tmp_path):
        from connector_service.bot import visio

        self._patch_transport(monkeypatch, [("p1", "Alice"), ("p2", "Bob"), ("p1", "Alice")])
        monkeypatch.setenv("TRANSCRIA_JOB_ID", "job-42")
        ingested = {}

        async def fake_ingest(url, token, occurrence, recording, job_id):
            ingested.update(job=job_id, tracks=len(recording.track_files()),
                            manifest=recording.to_manifest("visio"))
        monkeypatch.setattr(visio, "ingest_recording", fake_ingest)
        args = self._args(tmp_path)
        args.transcria_url = "http://portail"

        code = asyncio.run(visio.run(args, "clef", "secret"))

        assert code == 0
        assert ingested["job"] == "job-42" and ingested["tracks"] == 2   # une piste par voix
        assert ingested["manifest"]["version"] == 2

    def test_salle_jamais_occupee_code_1(self, monkeypatch, tmp_path):
        from connector_service.bot import visio

        self._patch_transport(monkeypatch, [])
        monkeypatch.delenv("TRANSCRIA_JOB_ID", raising=False)
        code = asyncio.run(visio.run(self._args(tmp_path), "clef", "secret"))
        assert code == 1                          # non admis : rejouer à l'identique = rien

    def test_connexion_en_echec_code_1(self, monkeypatch, tmp_path):
        from connector_service.bot import visio

        def boom(url, token):
            def make(_occ):
                async def gen():
                    raise RuntimeError("connexion LiveKit refusée")
                    yield  # pragma: no cover
                return gen()
            return make
        monkeypatch.setattr("connector_service.bot.visio.livekit_access_token",
                            lambda *a, **k: "jeton-factice")
        monkeypatch.setattr("connector_service.bot.visio.livekit_demux_source", boom)
        code = asyncio.run(visio.run(self._args(tmp_path), "clef", "secret"))
        assert code == 1                          # jamais entré → non admis, pas « technique »


@pytest.fixture(autouse=True)
def _resolution_stubbee(monkeypatch):
    """Les noms d'exemple (`.test`, `.exemple`) ne résolvent nulle part, et la garde
    sortante (S2.2, durcie après reprise d'audit) décide sur la DESTINATION : elle résout.
    On rend donc la résolution déterministe — sans quoi ces tests dépendraient du DNS de la
    machine qui les exécute."""
    import ipaddress

    import connector_service.outbound_guard as og

    def _stub(hote):
        try:
            ipaddress.ip_address(hote)
            return [hote]
        except ValueError:
            return ["93.184.216.34"]

    monkeypatch.setattr(og, "_resoudre", _stub)


class TestResolveLivekitRoom:
    """Vérifié dans la source officielle meet : salle ENREGISTRÉE → room = UUID
    (serializers.py:179) ; éphémère/injoignable → slug (viewsets.py:271)."""

    def test_salle_enregistree_room_uuid(self):
        from connector_service.bot.visio import resolve_livekit_room
        def opener(url):
            assert url == "https://visio.exemple/api/v1.0/rooms/ma-salle/"
            return 200, '{"livekit": {"room": "0aeb8887-1234"}}'
        assert resolve_livekit_room("https://visio.exemple/ma-salle", opener) == "0aeb8887-1234"

    def test_ephemere_ou_api_morte_repli_slug(self):
        from connector_service.bot.visio import resolve_livekit_room
        def dead(url):
            raise OSError("down")
        assert resolve_livekit_room("https://visio.exemple/ma-salle", dead) == "ma-salle"
        assert resolve_livekit_room("nom-brut", dead) == "nom-brut"


def test_api_base_surchargee_pour_la_stack_dev(monkeypatch):
    from connector_service.bot.visio import resolve_livekit_room
    monkeypatch.setenv("VISIO_API_BASE", "http://localhost:8071")
    seen = {}
    def opener(url):
        seen["url"] = url
        return 200, '{"livekit": {"room": "uuid-1"}}'
    assert resolve_livekit_room("http://localhost:3000/ma-salle", opener) == "uuid-1"
    assert seen["url"] == "http://localhost:8071/api/v1.0/rooms/ma-salle/"

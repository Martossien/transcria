"""Vague 4 — meeting-runner : config fail-loud, argv Docker purs, boucle du démon (fakes).

La boucle est testée SANS réseau ni Docker (portail = fonction injectée, bot = processus
factice) : claim borné par la capacité, relais des événements JSON, résultat posté avec le
code de sortie, annulation à chaud (SIGTERM), échec de lancement = code 3 (config).
"""
from __future__ import annotations

import asyncio

import pytest

from connector_service.runner.commands import docker_argv
from connector_service.runner.config import RunnerConfig, RunnerConfigError, load_runner_config
from connector_service.runner.daemon import MeetingRunnerDaemon

INTENT = {"session_id": "s1", "job_id": "j1", "provider": "jitsi",
          "meeting_ref": "https://meet.jit.si/x", "meeting_title": "T", "language": "fr",
          "attempt": 1}


class TestConfig:
    def test_fail_loud_sans_chemin(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_RUNNER_CONFIG", raising=False)
        with pytest.raises(RunnerConfigError, match="TRANSCRIA_RUNNER_CONFIG"):
            load_runner_config()

    def test_charge_valide(self, tmp_path, monkeypatch):
        token = tmp_path / "t.txt"
        token.write_text("tia_abc_def", encoding="utf-8")
        cfg_file = tmp_path / "runner.yaml"
        cfg_file.write_text(
            f"portal_url: http://127.0.0.1:7870\ntoken_file: {token}\n"
            "runner_name: r1\ncapacity: 3\nplatforms: [jitsi, zoom-sdk]\n", encoding="utf-8")
        cfg = load_runner_config(str(cfg_file))
        assert cfg.capacity == 3 and cfg.platforms == ("jitsi", "zoom-sdk")
        assert cfg.token == "tia_abc_def"

    def test_jeton_non_tia_refuse(self, tmp_path):
        token = tmp_path / "t.txt"
        token.write_text("pas-un-jeton", encoding="utf-8")
        cfg_file = tmp_path / "r.yaml"
        cfg_file.write_text(f"portal_url: http://x\ntoken_file: {token}\n", encoding="utf-8")
        with pytest.raises(RunnerConfigError, match="tia_"):
            load_runner_config(str(cfg_file))


class TestDockerArgv:
    def test_jamais_de_secret_dans_argv(self):
        argv, env = docker_argv(INTENT, portal_url="http://10.0.0.5:7870", token="tia_secret")
        assert "tia_secret" not in " ".join(argv)          # env only, jamais lisible dans ps
        assert env["TRANSCRIA_TOKEN"] == "tia_secret"
        assert env["BOT_EVENTS"] == "json" and env["TRANSCRIA_JOB_ID"] == "j1"
        assert "--network" not in argv                     # portail non-loopback : bridge

    def test_loopback_active_le_reseau_hote(self):
        argv, _ = docker_argv(INTENT, portal_url="http://127.0.0.1:7870", token="tia_x")
        assert "--network" in argv and "host" in argv

    def test_plateforme_inconnue_refusee(self):
        with pytest.raises(ValueError, match="image"):
            docker_argv(dict(INTENT, provider="webex"), portal_url="http://x", token="tia_x")


class _FakeProc:
    """Bot factice : émet des événements JSON puis sort avec le code voulu."""

    def __init__(self, exit_code=0, lines=(b'{"bot_event": "in_meeting"}\n',)):
        self._exit = exit_code
        self.terminated = False
        self.stdout = self._reader(lines)
        self._done = asyncio.Event()

    def _reader(self, lines):
        it = iter(lines)

        class R:
            async def readline(_self):
                try:
                    return next(it)
                except StopIteration:
                    return b""
        return R()

    def terminate(self):
        self.terminated = True
        self._done.set()

    async def wait(self):
        if self.terminated:
            return 0
        await asyncio.sleep(0)                     # laisse le relais lire stdout
        return self._exit


def _cfg(capacity=2):
    return RunnerConfig(portal_url="http://portal", token="tia_x",
                        runner_name="r-test", capacity=capacity, poll_interval_s=5.0)


def _portal(claims):
    """Portail factice : rejoue une liste de réponses de claim, enregistre tous les POST."""
    calls: list[tuple[str, dict]] = []

    async def post(path, payload):
        calls.append((path, payload))
        if path == "/v1/runners/heartbeat":
            return 200, {"ok": True, "cancelled_sessions": []}
        if path == "/v1/meetings/claim":
            return 200, {"sessions": claims.pop(0) if claims else []}
        return 200, {"ok": True}
    return post, calls


class TestDaemon:
    def test_cycle_complet_relaye_et_rapporte(self):
        async def scenario():
            post, calls = _portal([[INTENT]])
            proc = _FakeProc(exit_code=0)

            async def launch(intent):
                return proc
            daemon = MeetingRunnerDaemon(_cfg(), post=post, launch=launch)
            await daemon.run_once()
            await asyncio.gather(*daemon._active.values())
            return calls
        calls = asyncio.run(scenario())
        paths = [p for p, _ in calls]
        assert "/v1/meetings/s1/events" in paths            # l'événement JSON a été relayé
        result = next(pl for p, pl in calls if p.endswith("/result"))
        assert result["exit_code"] == 0

    def test_capacite_borne_le_claim(self):
        async def scenario():
            post, calls = _portal([[]])
            daemon = MeetingRunnerDaemon(_cfg(capacity=2), post=post, launch=None)
            daemon._active["occupee"] = asyncio.ensure_future(asyncio.sleep(0))
            await daemon.run_once()
            claim = next(pl for p, pl in calls if p == "/v1/meetings/claim")
            return claim["max"]
        assert asyncio.run(scenario()) == 1                 # 2 de capacité − 1 actif

    def test_lancement_impossible_code_3(self):
        async def scenario():
            post, calls = _portal([[INTENT]])

            async def launch(intent):
                raise RuntimeError("image absente")
            daemon = MeetingRunnerDaemon(_cfg(), post=post, launch=launch)
            await daemon.run_once()
            await asyncio.gather(*daemon._active.values())
            return next(pl for p, pl in calls if p.endswith("/result"))
        result = asyncio.run(scenario())
        assert result["exit_code"] == 3 and result["category"] == "launch"

    def test_annulation_a_chaud_termine_le_conteneur(self):
        async def scenario():
            proc = _FakeProc(exit_code=0, lines=())
            calls: list = []

            async def post(path, payload):
                calls.append(path)
                if path == "/v1/runners/heartbeat":
                    return 200, {"cancelled_sessions": ["s1"]}
                if path == "/v1/meetings/claim":
                    return 200, {"sessions": []}
                return 200, {}

            async def launch(intent):
                return proc
            daemon = MeetingRunnerDaemon(_cfg(), post=post, launch=launch)
            daemon._procs["s1"] = proc
            daemon._active["s1"] = asyncio.ensure_future(asyncio.sleep(0))
            await daemon.run_once()
            return proc.terminated
        assert asyncio.run(scenario()) is True

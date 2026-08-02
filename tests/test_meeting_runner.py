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
        assert "BOT_INITIATOR" in env
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


class TestCaptionRelay:
    """Vague 5, lot C : les lignes {"bot_caption": …} partent PAR LOTS vers
    /v1/meetings/<sid>/captions — flush à 25 tours, et TOUJOURS en fin de flux (les
    derniers mots d'une réunion ne restent jamais dans le tampon)."""

    def _run(self, lines):
        async def scenario():
            post, calls = _portal([[INTENT]])
            proc = _FakeProc(exit_code=0, lines=lines)

            async def launch(intent):
                return proc
            daemon = MeetingRunnerDaemon(_cfg(), post=post, launch=launch)
            await daemon.run_once()
            await asyncio.gather(*daemon._active.values())
            return calls
        return asyncio.run(scenario())

    def test_lot_final_part_a_la_fin_du_flux(self):
        calls = self._run((
            b'{"bot_event": "in_meeting"}\n',
            b'{"bot_caption": {"start": 1.0, "end": 2.0, "speaker": "Alice", "text": "Bonjour"}}\n',
            b'{"bot_caption": {"start": 3.0, "end": 4.0, "speaker": "", "text": "Oui"}}\n',
            b'ligne de diagnostic ordinaire\n',
        ))
        batches = [pl["captions"] for p, pl in calls if p.endswith("/captions")]
        assert len(batches) == 1                               # un seul lot : flush de fin
        assert [c["text"] for c in batches[0]] == ["Bonjour", "Oui"]
        assert any(p.endswith("/events") and pl.get("event") == "in_meeting"
                   for p, pl in calls)                         # les états passent toujours

    def test_lot_plein_part_sans_attendre(self):
        lines = tuple(
            b'{"bot_caption": {"start": 0, "end": 1, "text": "tour %d"}}\n' % i
            for i in range(26)
        )
        calls = self._run(lines)
        batches = [pl["captions"] for p, pl in calls if p.endswith("/captions")]
        assert [len(b) for b in batches] == [25, 1]            # 25 pleins + le reliquat final


class TestPlatformEnvDuClaim:
    def test_valeurs_dans_l_env_jamais_dans_argv(self):
        intent = dict(INTENT, platform_env={"ZOOM_CLIENT_ID": "abc", "ZOOM_CLIENT_SECRET": "s3cret"})
        argv, env = docker_argv(intent, portal_url="http://127.0.0.1:7870", token="tia_x")
        assert env["ZOOM_CLIENT_ID"] == "abc" and env["ZOOM_CLIENT_SECRET"] == "s3cret"
        assert "s3cret" not in " ".join(argv)      # argv ne porte que `-e NOM`
        assert "-e" in argv and "ZOOM_CLIENT_SECRET" in argv


def test_zoom_meeting_ref_par_env_jamais_argv():
    """Vécu au premier gate Zoom via runner : le bot SDK n'a pas de positionnel
    (« unrecognized arguments » en boucle) — et le lien porte un ?pwd= qui ne doit
    jamais apparaître dans `ps`."""
    intent = dict(INTENT, provider="zoom-sdk",
                  meeting_ref="https://us05web.zoom.us/j/123?pwd=SECRET.1")
    argv, env = docker_argv(intent, portal_url="http://127.0.0.1:7870", token="tia_x")
    assert env["ZOOM_MEETING"] == "https://us05web.zoom.us/j/123?pwd=SECRET.1"
    assert not any("zoom.us" in a for a in argv)      # jamais le lien dans argv
    assert argv[-1] == "transcria-zoom-sdk:latest"    # l'image reste le dernier argument


# --- Sécurité : l'allowlist sortante doit ATTEINDRE le conteneur -------------------------
#
# `VISIO_ALLOWED_HOSTS` est lue par la garde qui tourne DANS le bot. Posée sur l'hôte mais
# non relayée, la garde y voit une liste vide et ne borne rien : la protection existe et ne
# s'applique jamais. Le même oubli s'était déjà produit pour `BOT_IDLE_TIMEOUT_S` — le
# commentaire de `_MACHINE_ENV` en garde la trace.

def test_lallowlist_sortante_est_relayee_au_conteneur(monkeypatch):
    from connector_service.runner.commands import docker_argv

    monkeypatch.setenv("VISIO_ALLOWED_HOSTS", "visio.exemple.test,autre.hote")
    argv, env = docker_argv(
        {"provider": "visio", "job_id": "j1", "meeting_ref": "https://visio.exemple/salle"},
        portal_url="https://portail.exemple", token="tia_x")
    assert env.get("VISIO_ALLOWED_HOSTS") == "visio.exemple.test,autre.hote"
    assert "VISIO_ALLOWED_HOSTS" in argv          # transmise par `-e NOM`
    # la VALEUR ne doit jamais apparaître dans argv (visible de tout `ps`)
    assert "visio.exemple.test,autre.hote" not in argv


def test_sans_allowlist_rien_nest_transmis(monkeypatch):
    """Contre-épreuve : on ne pose pas une variable vide dans le conteneur."""
    from connector_service.runner.commands import docker_argv

    monkeypatch.delenv("VISIO_ALLOWED_HOSTS", raising=False)
    _argv, env = docker_argv(
        {"provider": "visio", "job_id": "j1", "meeting_ref": "https://visio.exemple/salle"},
        portal_url="https://portail.exemple", token="tia_x")
    assert "VISIO_ALLOWED_HOSTS" not in env


def test_lunite_systemd_du_runner_charge_un_fichier_denvironnement():
    """Sans `EnvironmentFile`, tout `_MACHINE_ENV` est INERTE sous l'installation nominale.

    `VISIO_ALLOWED_HOSTS`, `VISIO_API_BASE`, `BOT_HIDDEN`… sont lues dans l'environnement
    du process runner pour être relayées au conteneur. systemd ne peuple pas cet
    environnement depuis `.env` tout seul : le relais fonctionnait, la source était vide."""
    from transcria.ingestion.runner_kit import build_kit_script

    script = build_kit_script(portal_url="https://portail.exemple",
                              token="tia_x", runner_name="salle-a")
    assert "EnvironmentFile=-" in script, "l'unité doit charger un fichier d'environnement"


def test_lunite_posee_par_linstalleur_charge_aussi(tmp_path):
    from transcria.installer.connectors_phase import _unit_text

    texte = _unit_text(repo_root="/opt/transcria", venv_python="/opt/transcria/venv/bin/python",
                       config_path="/etc/transcria/runner.yaml")
    assert "EnvironmentFile=-" in texte

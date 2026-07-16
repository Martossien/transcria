"""Chemins « processus réel » des backends LLM (préparation B3, plan §3.14).

GPU-free : les coutures sous-processus sont substituées AU CONSOMMATEUR
(``llm_backend.subprocess``, ``os.kill``, ``is_port_open``) — aucun serveur,
aucun kill réel. Ces chemins (lancement script, arrêt, kill de port, attente de
port, diagnostic de panne) étaient les 141 lignes mortes qui interdisaient de
refactorer la zone GPU (B3) : ils sont désormais sous filet.
"""
import subprocess as real_subprocess
import time
from types import SimpleNamespace

import requests

import transcria.gpu.llm_backend as lb
from transcria.gpu.llm_backend import (
    HTTPLLMBackend,
    LLMBackend,
    OllamaLLMBackend,
    ScriptLLMBackend,
    create_llm_backend,
)


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _script_config(tmp_path, *, script_exists=True):
    script = tmp_path / "launch.sh"
    stop = tmp_path / "stop.sh"
    if script_exists:
        script.write_text("#!/bin/bash\ntrue\n")
    stop.write_text("#!/bin/bash\ntrue\n")
    return {
        "services": {
            "backend": "script",
            "arbitrage_script": str(script),
            "stop_script": str(stop),
            "arbitrage_llm_port": 18080,
            "arbitrage_log_path": str(tmp_path / "launch.log"),
        },
        "workflow": {"arbitration_llm": {"model_id": "local/arbitrage"}},
    }


def _ollama_config(model_id="qwen3:8b"):
    return {
        "services": {"backend": "ollama", "ollama_url": "http://127.0.0.1:11434"},
        "workflow": {"arbitration_llm": {"model_id": model_id}},
    }


# ── Fabrique : toutes les branches ────────────────────────────────────────

class TestFactoryBranches:
    def test_explicit_script(self, tmp_path):
        assert isinstance(create_llm_backend(_script_config(tmp_path)), ScriptLLMBackend)

    def test_explicit_http_reads_llm_port(self):
        cfg = {"services": {"backend": "http"},
               "workflow": {"arbitration_llm": {"port": 9999}}}
        b = create_llm_backend(cfg)
        assert isinstance(b, HTTPLLMBackend) and b.port == 9999

    def test_fallback_ollama_url(self):
        b = create_llm_backend({"services": {"ollama_url": "http://x:11434"}}, backend_type="autre")
        assert isinstance(b, OllamaLLMBackend)

    def test_fallback_script_then_http(self, tmp_path):
        cfg = _script_config(tmp_path)
        cfg["services"]["backend"] = "inconnu"
        assert isinstance(create_llm_backend(cfg, backend_type="inconnu"), ScriptLLMBackend)
        assert isinstance(create_llm_backend({"services": {}}, backend_type="inconnu"), HTTPLLMBackend)


# ── Diagnostic de panne de lancement ──────────────────────────────────────

class TestDiagnosticTail:
    def test_no_path_and_missing_file(self, tmp_path):
        assert "aucun log" in LLMBackend._diagnostic_tail(None)
        assert "aucun log" in LLMBackend._diagnostic_tail(str(tmp_path / "absent.log"))

    def test_empty_then_content(self, tmp_path):
        log = tmp_path / "l.log"
        log.write_text("")
        assert "vide" in LLMBackend._diagnostic_tail(str(log))
        log.write_text("\n".join(f"ligne {i}" for i in range(40)) + "\n")
        tail = LLMBackend._diagnostic_tail(str(log), n_lines=25)
        assert "ligne 39" in tail and "ligne 10" not in tail

    def test_unreadable_path_is_reported_not_raised(self, tmp_path):
        # Un répertoire n'est pas un fichier régulier → branche « aucun log ».
        assert "aucun log" in LLMBackend._diagnostic_tail(str(tmp_path))


# ── Attente du port ───────────────────────────────────────────────────────

class TestWaitForPort:
    def test_port_opens_after_polls(self, monkeypatch):
        calls = {"n": 0}

        def probe(port, timeout=5):
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(probe))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert LLMBackend._wait_for_port(18080, timeout=60) is True
        assert calls["n"] == 3

    def test_early_process_death_short_circuits(self, monkeypatch, tmp_path):
        log = tmp_path / "l.log"
        log.write_text("CUDA error: out of memory\n")
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        proc = SimpleNamespace(poll=lambda: 1, returncode=1)
        assert LLMBackend._wait_for_port(18080, timeout=60, proc=proc, log_path=str(log)) is False

    def test_timeout_without_process(self, monkeypatch):
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert LLMBackend._wait_for_port(18080, timeout=0) is False


# ── ScriptLLMBackend : lancement ──────────────────────────────────────────

class _FakePopen:
    """Simule le serveur lancé (start_new_session, sortie redirigée)."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242

    def poll(self):
        return None


class TestScriptEnsureAvailable:
    def test_already_available_short_circuits(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))
        monkeypatch.setattr(b, "is_available", lambda: True)
        b._launched_by_us = True
        assert b.ensure_available() is True
        assert b._launched_by_us is False   # résident ≠ lancé par nous

    def test_missing_launch_script_fails(self, tmp_path):
        b = ScriptLLMBackend(_script_config(tmp_path, script_exists=False))
        assert b.ensure_available() is False

    def test_launch_success_records_pid_and_ownership(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))
        launched = {}

        def popen(cmd, **kwargs):
            launched["cmd"] = cmd
            launched["kwargs"] = kwargs
            return _FakePopen(cmd, **kwargs)

        monkeypatch.setattr(lb.subprocess, "Popen", popen)
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        monkeypatch.setattr(LLMBackend, "_wait_for_port",
                            staticmethod(lambda port, timeout=300, proc=None, log_path=None: True))
        assert b.ensure_available() is True
        assert b._pid == 4242 and b._launched_by_us is True
        assert launched["cmd"][0] == "/bin/bash" and launched["kwargs"]["start_new_session"] is True
        # La sortie du lancement est capturée dans le log déclaré.
        assert (tmp_path / "launch.log").exists()

    def test_stale_port_is_cleaned_before_launch(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))
        killed = {}
        # 1er is_available()=False (pas de LLM), puis sonde de nettoyage=True (port squatté).
        monkeypatch.setattr(b, "is_available", lambda: False)
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: True))
        monkeypatch.setattr(ScriptLLMBackend, "_kill_port",
                            staticmethod(lambda port: killed.setdefault("port", port) or True))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(lb.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(LLMBackend, "_wait_for_port",
                            staticmethod(lambda port, timeout=300, proc=None, log_path=None: True))
        assert b.ensure_available() is True
        assert killed["port"] == b.port

    def test_unwritable_log_falls_back_to_devnull(self, tmp_path, monkeypatch):
        cfg = _script_config(tmp_path)
        cfg["services"]["arbitrage_log_path"] = str(tmp_path)   # répertoire → open() échoue
        b = ScriptLLMBackend(cfg)
        seen = {}

        def popen(cmd, **kwargs):
            seen["stdout"] = kwargs["stdout"]
            return _FakePopen(cmd, **kwargs)

        monkeypatch.setattr(lb.subprocess, "Popen", popen)
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        monkeypatch.setattr(LLMBackend, "_wait_for_port",
                            staticmethod(lambda port, timeout=300, proc=None, log_path=None: True))
        assert b.ensure_available() is True
        assert seen["stdout"] is real_subprocess.DEVNULL

    def test_popen_failure_returns_false(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))

        def boom(cmd, **kwargs):
            raise OSError("fork interdit")

        monkeypatch.setattr(lb.subprocess, "Popen", boom)
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        assert b.ensure_available() is False


# ── ScriptLLMBackend : arrêt ──────────────────────────────────────────────

class TestScriptShutdown:
    def test_not_ours_is_noop(self, tmp_path):
        b = ScriptLLMBackend(_script_config(tmp_path))
        b._launched_by_us = False
        assert b.shutdown() is True

    def test_shutdown_kills_pid_runs_stop_script_and_frees_port(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))
        b._launched_by_us = True
        b._pid = 4242
        events = []
        monkeypatch.setattr(lb.os, "kill", lambda pid, sig: events.append(("kill", pid, sig)))
        monkeypatch.setattr(lb.subprocess, "run",
                            lambda cmd, **kw: events.append(("run", cmd[1])) or SimpleNamespace(stdout=""))
        monkeypatch.setattr(ScriptLLMBackend, "_kill_port",
                            staticmethod(lambda port: events.append(("kill_port", port)) or True))
        assert b.shutdown() is True
        assert ("kill", 4242, 15) in events
        assert ("run", b.stop_script) in events
        assert ("kill_port", b.port) in events
        assert b._launched_by_us is False and b._pid is None

    def test_shutdown_survives_stop_script_failure(self, tmp_path, monkeypatch):
        b = ScriptLLMBackend(_script_config(tmp_path))
        b._launched_by_us = True

        def boom(cmd, **kw):
            raise real_subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(lb.subprocess, "run", boom)
        monkeypatch.setattr(ScriptLLMBackend, "_kill_port", staticmethod(lambda port: True))
        assert b.shutdown() is True


# ── ScriptLLMBackend : kill de port (logique lsof) ────────────────────────

class TestKillPort:
    def _lsof(self, outputs):
        """Simule les DEUX appels lsof successifs (avant/après SIGTERM)."""
        it = iter(outputs)

        def run(cmd, **kw):
            return SimpleNamespace(stdout=next(it))

        return run

    def test_no_listener_is_true(self, monkeypatch):
        monkeypatch.setattr(lb.subprocess, "run", self._lsof(["\n"]))
        assert ScriptLLMBackend._kill_port(18080) is True

    def test_sigterm_then_sigkill_for_survivors(self, monkeypatch):
        signals = []
        monkeypatch.setattr(lb.subprocess, "run", self._lsof(["123\n456\n", "456\n"]))
        monkeypatch.setattr(lb.os, "kill", lambda pid, sig: signals.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert ScriptLLMBackend._kill_port(18080) is True
        assert (123, 15) in signals and (456, 15) in signals   # SIGTERM aux deux
        assert (456, 9) in signals and (123, 9) not in signals  # SIGKILL au seul survivant

    def test_dead_pid_is_tolerated(self, monkeypatch):
        def kill(pid, sig):
            raise ProcessLookupError(pid)

        monkeypatch.setattr(lb.subprocess, "run", self._lsof(["123\n", "\n"]))
        monkeypatch.setattr(lb.os, "kill", kill)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert ScriptLLMBackend._kill_port(18080) is True

    def test_lsof_failure_returns_false(self, monkeypatch):
        def boom(cmd, **kw):
            raise FileNotFoundError("lsof absent")

        monkeypatch.setattr(lb.subprocess, "run", boom)
        assert ScriptLLMBackend._kill_port(18080) is False


# ── Ollama : chargement/déchargement HTTP ─────────────────────────────────

class TestOllamaEnsureAvailable:
    def test_not_pulled_warns_and_fails(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(200, {"models": []}))
        assert b.ensure_available() is False

    def test_already_loaded_short_circuits(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(
            requests, "get",
            lambda url, timeout=5: _FakeResp(200, {"models": [{"name": "qwen3:8b", "size_vram": 1 << 30}]}),
        )
        assert b.ensure_available() is True

    def test_loads_via_empty_generate(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        posted = {}

        def fake_get(url, timeout=5):
            if url.endswith("/api/tags"):
                return _FakeResp(200, {"models": [{"name": "qwen3:8b"}]})
            return _FakeResp(200, {"models": []})   # /api/ps : pas encore résident

        def fake_post(url, json=None, timeout=None):
            posted.update(url=url, json=json)
            return _FakeResp(200, {})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        assert b.ensure_available() is True
        assert posted["url"].endswith("/api/generate") and posted["json"]["model"] == "qwen3:8b"

    def test_load_http_error_and_exception_fail(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())

        def fake_get(url, timeout=5):
            if url.endswith("/api/tags"):
                return _FakeResp(200, {"models": [{"name": "qwen3:8b"}]})
            return _FakeResp(200, {"models": []})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: _FakeResp(500, {}))
        assert b.ensure_available() is False

        def boom(url, json=None, timeout=None):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "post", boom)
        assert b.ensure_available() is False

    def test_measured_vram_sums_resident_models(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(
            requests, "get",
            lambda url, timeout=5: _FakeResp(200, {"models": [
                {"name": "qwen3:8b", "size_vram": 2 * (1 << 30)},
                {"name": "autre:1b", "size_vram": 1 << 30},
            ]}),
        )
        assert b.measured_vram_mb() == 2048

    def test_measured_vram_none_when_unreachable_or_absent(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(503, {}))
        assert b.measured_vram_mb() is None
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(200, {"models": []}))
        assert b.measured_vram_mb() is None

    def test_unload_error_paths(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: _FakeResp(500, {}))
        assert b.unload() is False

        def boom(url, json=None, timeout=None):
            raise requests.exceptions.ReadTimeout("lent")

        monkeypatch.setattr(requests, "post", boom)
        assert b.unload() is False

    def test_base_url_and_model_id_fallback(self):
        b = OllamaLLMBackend(_ollama_config(model_id="local/qwen3:8b"))
        assert b.base_url == "http://127.0.0.1:11434/v1"
        assert b.model_id == "qwen3:8b"   # préfixe provider opencode retiré


# ── HTTPLLMBackend ────────────────────────────────────────────────────────

class TestHttpBackend:
    def _cfg(self, **llm):
        return {"services": {"arbitrage_llm_port": 18080},
                "workflow": {"arbitration_llm": {"model_id": "local/arbitrage", **llm}}}

    def test_base_url_prefers_api_base(self):
        assert HTTPLLMBackend(self._cfg(api_base="http://gpu-node:8080/v1")).base_url == "http://gpu-node:8080/v1"
        assert HTTPLLMBackend(self._cfg()).base_url == "http://127.0.0.1:18080/v1"

    def test_availability_and_shutdown(self, monkeypatch):
        b = HTTPLLMBackend(self._cfg())
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: True))
        assert b.is_available() is True and b.ensure_available() is True
        monkeypatch.setattr(LLMBackend, "is_port_open", staticmethod(lambda p, timeout=5: False))
        assert b.ensure_available() is False   # http : jamais de lancement, juste un warning
        assert b.shutdown() is True
        assert b.model_id == "local/arbitrage"

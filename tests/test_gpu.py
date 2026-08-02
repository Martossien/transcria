"""Tests for VRAM Manager — unit tests with mocked subprocess/requests for GPU lifecycle."""
import os
import signal
import subprocess
import time

import pytest

from transcria.gpu.cuda_visible import (
    parse_cuda_visible_devices,
    to_nvidia_smi_gpu_index,
    to_visible_device_index,
)
import pathlib

from transcria.gpu.vram_manager import VRAMManager


def _default_config(**overrides):
    cfg = {
        "services": {
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "qwen_port": 8080,
            "llm_cleanup_ports": [8000],
        },
    }
    for k, v in overrides.items():
        cfg["services"][k] = v
    # S1.6 : les racines autorisées viennent de l'ENVIRONNEMENT, plus de la config (une
    # allowlist réglable par l'admin ne le contraint pas). Ces tests se servent de
    # `/bin/true` comme lanceur factice, et parfois d'un script en tmp_path.
    racines = ["/bin"] + [str(pathlib.Path(overrides[c]).parent)
                          for c in ("arbitrage_script", "stop_script") if overrides.get(c)]
    os.environ["TRANSCRIA_SCRIPT_ROOTS"] = ":".join(racines)
    return cfg


def _fake_gpu_info(gpus):
    """Return a function that returns predetermined GPU info."""
    def getter(self):
        return gpus
    return getter


class TestCudaVisibleDevices:
    def test_unset_cuda_visible_devices_is_unconstrained(self, monkeypatch):
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
        assert parse_cuda_visible_devices() is None
        assert to_visible_device_index(3) == 3
        assert to_nvidia_smi_gpu_index(3) == 3

    def test_disabled_cuda_visible_devices_masks_all_gpus(self, monkeypatch):
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
        assert parse_cuda_visible_devices() == []
        assert to_visible_device_index(0) is None

    def test_physical_ids_are_remapped_to_visible_ordinals(self, monkeypatch):
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2,5")
        assert to_visible_device_index(2) == 0
        assert to_visible_device_index(5) == 1
        assert to_visible_device_index(1) is None
        assert to_visible_device_index(1, allow_remapped_ordinal=True) == 1
        assert to_nvidia_smi_gpu_index(1) == 5


class TestVRAMManagerInstantiation:
    def test_instantiation(self):
        mgr = VRAMManager(config=_default_config())
        assert mgr is not None
        # C2.3 : llmdashboard retiré — plus d'attribut dashboard_url.
        assert not hasattr(mgr, "dashboard_url")

    def test_config_overrides(self):
        cfg = _default_config(arbitrage_llm_port=9999, llm_cleanup_ports=[8888])
        mgr = VRAMManager(config=cfg)
        assert mgr.arbitrage_llm_port == 9999
        assert mgr.llm_cleanup_ports == [8888]

    def test_script_paths_from_config(self):
        cfg = _default_config()
        mgr = VRAMManager(config=cfg)
        assert mgr.arbitrage_script == "/bin/true"
        assert mgr.stop_script == "/bin/true"

    def test_env_var_overrides_scripts(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIA_ARBITRAGE_SCRIPT", "/custom/arb.sh")
        monkeypatch.setenv("TRANSCRIA_STOP_SCRIPT", "/custom/stop.sh")
        mgr = VRAMManager(config=_default_config())
        assert mgr.arbitrage_script == "/custom/arb.sh"
        assert mgr.stop_script == "/custom/stop.sh"

    def test_vram_defaults(self):
        cfg = {}
        mgr = VRAMManager(config=cfg)
        assert mgr.pyannote_vram_mb > 0
        assert mgr.min_free_mb > 0


class TestVRAMManagerGetGpuInfo:
    def test_get_gpu_info_source_locale(self, monkeypatch):
        # C2.3 : la source est LOCALE (torch) — aucun appel réseau ne doit partir.
        import requests

        def _no_network(*a, **kw):
            raise AssertionError("get_gpu_info ne doit plus faire d'appel réseau (C2.3)")

        monkeypatch.setattr(requests, "get", _no_network)
        mgr = VRAMManager(config=_default_config())
        gpus = mgr.get_gpu_info()
        assert isinstance(gpus, list)   # torch absent/CPU → liste vide, jamais d'exception

    def test_get_gpu_info_ne_leve_jamais(self):
        mgr = VRAMManager(config=_default_config())
        assert isinstance(mgr.get_gpu_info(), list)

    def test_get_gpu_info_fallback_on_http_error(self, monkeypatch):
        import requests
        def raise_status(*a, **kw):
            r = type("R", (), {"status_code": 500, "raise_for_status": lambda self: (_ for _ in ()).throw(requests.HTTPError("500"))})()
            return r
        monkeypatch.setattr(requests, "get", raise_status)
        mgr = VRAMManager(config=_default_config())
        gpus = mgr.get_gpu_info()
        assert isinstance(gpus, list)


class TestVRAMManagerGetFreeVram:
    def test_get_free_vram_mb_from_mocked_info(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(VRAMManager, "get_gpu_info", lambda self: [
            {"id": 0, "memory": {"free": 20.0, "total": 24.0, "used": 4.0}},
        ])
        free = mgr.get_free_vram_mb(0)
        assert free == int(20.0 * 1024)

    def test_get_free_vram_mb_missing_gpu(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(VRAMManager, "get_gpu_info", lambda self: [
            {"id": 0, "memory": {"free": 20.0, "total": 24.0, "used": 4.0}},
        ])
        free = mgr.get_free_vram_mb(99)
        assert free == 0

    def test_get_free_vram_mb_empty_gpus(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(VRAMManager, "get_gpu_info", lambda self: [])
        free = mgr.get_free_vram_mb(0)
        assert free == 0


class TestVRAMManagerFreeMemory:
    def test_free_memory_kills_large_processes(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        nvidia_output = "12345, llama-server, 8000\n67890, tiny_app, 500\n"
        second_output = "12345, llama-server, 8000\n"

        call_n = {"n": 0}

        def fake_run(cmd, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout=nvidia_output, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=second_output, stderr="")

        killed_pids = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)
        assert (12345, signal.SIGTERM) in killed_pids

    def test_free_memory_targets_requested_visible_gpu(self, monkeypatch):
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,2")
        mgr = VRAMManager(config=_default_config())
        commands = []

        def fake_run(cmd, **kw):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(1)

        assert commands
        assert all(cmd[1:3] == ["-i", "2"] for cmd in commands)

    def test_free_memory_skips_small_processes(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        nvidia_output = "11111, app, 500\n22222, app, 200\n"

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=nvidia_output, stderr="")

        killed_pids = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)
        assert len(killed_pids) == 0

    def test_free_memory_empty_nvidia_output(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)

    def test_free_memory_malformed_lines_skipped(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        nvidia_output = "badline\n,,,,\n33333, llama-server, 7000\n"

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=nvidia_output, stderr="")

        killed_pids = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)
        assert (33333, signal.SIGTERM) in killed_pids
        bad_pids = [pid for pid, _ in killed_pids if pid not in (33333,)]
        assert len(bad_pids) == 0

    def test_free_memory_sigkill_after_sigterm_failure(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        first_output = "44444, llama-server, 9000\n"
        second_output = "44444, llama-server, 9000\n"

        call_n = {"n": 0}

        def fake_run(cmd, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout=first_output, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=second_output, stderr="")

        killed_with = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_with.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)
        assert (44444, signal.SIGTERM) in killed_with
        assert (44444, signal.SIGKILL) in killed_with

    def test_free_memory_subprocess_exception_is_caught(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(subprocess, "run",lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=[], timeout=10)))
        mgr._free_memory(0)

    def test_free_memory_pid_1_not_killed(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        nvidia_output = "1, llama-server, 50000\n99999, llama-server, 8000\n"

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=nvidia_output, stderr="")

        killed_pids = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)
        assert 1 not in [pid for pid, _ in killed_pids]
        assert (99999, signal.SIGTERM) in killed_pids

    def test_free_memory_does_not_kill_unmatched_processes(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        nvidia_output = "22222, python, 12000\n"

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=nvidia_output, stderr="")

        killed_pids = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr._free_memory(0)

        assert killed_pids == []


class TestVRAMManagerKillPort:
    def test_kill_port_no_process(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        result = mgr._kill_port(8080)
        assert result is True

    def test_kill_port_one_process_clean_exit(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        call_n = {"n": 0}

        def fake_run(cmd, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout="1234\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        killed = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr._kill_port(8080)
        assert result is True
        assert (1234, signal.SIGTERM) in killed

    def test_kill_port_process_resists_then_sigkill(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        call_n = {"n": 0}

        def fake_run(cmd, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout="5555\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="5555\n", stderr="")

        killed = []
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr._kill_port(8080)
        assert result is True
        assert (5555, signal.SIGKILL) in killed

    def test_kill_port_process_gone_before_kill(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        call_n = {"n": 0}

        def fake_run(cmd, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout="7777\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        def fake_kill(pid, sig):
            raise ProcessLookupError(f"No process {pid}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr._kill_port(8080)
        assert result is True

    def test_kill_port_permission_error_handled(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="8888\n", stderr="")

        def fake_kill(pid, sig):
            raise PermissionError("Not allowed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr._kill_port(8080)
        assert result is True

    def test_kill_port_generic_exception(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())

        def fake_run(cmd, **kw):
            raise OSError("Subprocess failed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr._kill_port(8080)
        assert result is False


class TestVRAMManagerIsPortOpen:
    def test_port_open_model_responds(self, monkeypatch):
        import requests

        def fake_get(url, **kw):
            r = type("R", (), {"status_code": 200, "json": lambda self: {"data": [{"id": "test-llm"}]}})()
            r.raise_for_status = lambda: None
            return r

        def fake_post(url, **kw):
            return type("R", (), {
                "status_code": 200,
                "json": lambda self: {"choices": [{"text": "Bonjour"}]},
            })()

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)

        result = VRAMManager.is_port_open(8080)
        assert result is True

    def test_port_open_model_empty_data(self, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": []}})()
        def fake_post(url, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {"choices": []}})()

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)

        result = VRAMManager.is_port_open(8080)
        assert result is False

    def test_port_open_connection_error(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("refused")))

        result = VRAMManager.is_port_open(8080)
        assert result is False

    def test_port_open_http_error(self, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return type("R", (), {"status_code": 500, "json": lambda self: {}, "raise_for_status": lambda self: (_ for _ in ()).throw(requests.HTTPError("500"))})()

        monkeypatch.setattr(requests, "get", fake_get)

        result = VRAMManager.is_port_open(8080)
        assert result is False

    def test_port_open_inference_returns_empty_text(self, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": [{"id": "model"}]}})()

        def fake_post(url, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {"choices": [{"text": ""}]}})()

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)

        result = VRAMManager.is_port_open(8080)
        assert result is False


class TestVRAMManagerArbitrageRunning:
    def test_arbitrage_running_uses_api_health_before_lsof(self, monkeypatch):
        mgr = VRAMManager(config=_default_config(arbitrage_llm_port=8080))
        calls = []

        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: True))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a) or None)

        assert mgr.is_arbitrage_llm_running() is True
        assert calls == []

    def test_arbitrage_running_falls_back_to_lsof(self, monkeypatch):
        mgr = VRAMManager(config=_default_config(arbitrage_llm_port=8080))

        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: type("R", (), {"stdout": "123\n"})(),
        )

        assert mgr.is_arbitrage_llm_running() is True


class TestVRAMManagerWaitForPort:
    def test_wait_for_port_immediate_success(self, monkeypatch):
        monkeypatch.setattr(VRAMManager, "is_port_open", lambda port: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        result = VRAMManager._wait_for_port(8080, timeout=5)
        assert result is True

    def test_wait_for_port_success_after_retries(self, monkeypatch):
        attempts = {"n": 0}

        def fake_is_open(port):
            attempts["n"] += 1
            return attempts["n"] >= 3

        monkeypatch.setattr(VRAMManager, "is_port_open", fake_is_open)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        result = VRAMManager._wait_for_port(8080, timeout=30)
        assert result is True
        assert attempts["n"] >= 3

    def test_wait_for_port_timeout(self, monkeypatch):
        monkeypatch.setattr(VRAMManager, "is_port_open", lambda port: False)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(time, "time", lambda: 0)
        result = VRAMManager._wait_for_port(8080, timeout=0)
        assert result is False

    def test_wait_for_port_detects_early_process_death(self, monkeypatch, caplog, tmp_path):
        """Si le serveur sort avant d'ouvrir le port, on n'attend pas tout le
        timeout et on remonte code de sortie + tail du log (le point aveugle
        du rapport : LLM down sans aucun log explicatif)."""
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        sleeps = {"n": 0}
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.__setitem__("n", sleeps["n"] + 1))

        log_file = tmp_path / "arbitrage.log"
        log_file.write_text("llama_model_load: error loading model: failed to open GGUF\n")

        class DeadProc:
            returncode = 1

            def poll(self):
                return 1  # déjà mort

        with caplog.at_level("ERROR"):
            result = VRAMManager._wait_for_port(
                8080, timeout=600, proc=DeadProc(), log_path=str(log_file)
            )
        assert result is False
        assert sleeps["n"] == 0  # sortie immédiate, pas d'attente du timeout
        assert "s'est arrêté avant d'ouvrir le port" in caplog.text
        assert "failed to open GGUF" in caplog.text

    def test_wait_for_port_timeout_includes_log_tail(self, monkeypatch, caplog, tmp_path):
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(time, "time", lambda: 0)

        log_file = tmp_path / "arbitrage.log"
        log_file.write_text("CUDA error: out of memory\n")

        with caplog.at_level("ERROR"):
            result = VRAMManager._wait_for_port(8080, timeout=0, log_path=str(log_file))
        assert result is False
        assert "out of memory" in caplog.text

    def test_diagnostic_tail_handles_missing_and_empty(self, tmp_path):
        assert "sortie non capturée" in VRAMManager._diagnostic_tail(None)
        assert "sortie non capturée" in VRAMManager._diagnostic_tail("")
        missing = tmp_path / "nope.log"
        assert "aucun log de lancement disponible" in VRAMManager._diagnostic_tail(str(missing))
        empty = tmp_path / "empty.log"
        empty.write_text("")
        assert "vide" in VRAMManager._diagnostic_tail(str(empty))

    def test_launch_arbitrage_captures_output_to_log(self, monkeypatch, tmp_path):
        """Le lancement doit rediriger stdout/stderr vers le fichier de log
        configuré (plus de DEVNULL silencieux)."""
        log_file = tmp_path / "arb.log"
        mgr = VRAMManager(config=_default_config(arbitrage_log_path=str(log_file)))
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(VRAMManager, "_wait_for_port", staticmethod(lambda port, timeout=600, **kw: True))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        captured = {}

        class FakePopen:
            pid = 4242

            def __init__(self, *a, **kw):
                captured["stdout"] = kw.get("stdout")
                captured["stderr"] = kw.get("stderr")

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        result = mgr.launch_arbitrage_llm()
        assert result is True
        # stdout/stderr pointent vers un fichier ouvert (pas DEVNULL)
        assert captured["stdout"] is not subprocess.DEVNULL
        assert captured["stdout"] is captured["stderr"]
        assert mgr.arbitrage_log_path == str(log_file)


class TestVRAMManagerLaunchArbitrageLLM:
    def test_launch_arbitrage_script_not_found(self, monkeypatch):
        mgr = VRAMManager(config=_default_config(arbitrage_script="/nonexistent/script.sh"))
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        result = mgr.launch_arbitrage_llm()
        assert result is False

    def test_launch_arbitrage_script_exists_and_launches(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(VRAMManager, "_wait_for_port", staticmethod(lambda port, timeout=600, **kw: True))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        launched = {"done": False}

        class FakePopen:
            pid = 12345

            def __init__(self, *a, **kw):
                launched["done"] = True

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        result = mgr.launch_arbitrage_llm()
        assert result is True
        assert launched["done"]

    def test_launch_arbitrage_kills_existing_port_then_launches(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        check_n = {"n": 0}

        def fake_is_port_open(port):
            check_n["n"] += 1
            return check_n["n"] <= 1

        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(fake_is_port_open))
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: True)
        monkeypatch.setattr(VRAMManager, "_wait_for_port", staticmethod(lambda port, timeout=600, **kw: True))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        class FakePopen:
            pid = 54321
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        result = mgr.launch_arbitrage_llm()
        assert result is True

    def test_launch_arbitrage_popen_exception(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        def fail_popen(*a, **kw):
            raise OSError("Cannot fork")

        monkeypatch.setattr(subprocess, "Popen", fail_popen)
        result = mgr.launch_arbitrage_llm()
        assert result is False

    def test_launch_arbitrage_wait_timeout(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(VRAMManager, "is_port_open", staticmethod(lambda port: False))
        monkeypatch.setattr(VRAMManager, "_wait_for_port", staticmethod(lambda port, timeout=600, **kw: False))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        class FakePopen:
            pid = 99999

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        result = mgr.launch_arbitrage_llm()
        assert result is False


class TestVRAMManagerStopArbitrageLLM:
    def test_stop_arbitrage_runs_script_and_kills_port(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: True)

        script_called = {"done": False}

        def fake_run(cmd, **kw):
            script_called["done"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr.stop_arbitrage_llm()
        assert result is True
        assert script_called["done"]

    def test_stop_arbitrage_script_not_found_falls_back_to_kill_port(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr.stop_arbitrage_llm()
        assert result is True

    def test_stop_arbitrage_script_exception_falls_back(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("fail")))
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = mgr.stop_arbitrage_llm()
        assert result is True

    def test_stop_arbitrage_resets_pid(self, monkeypatch):
        mgr = VRAMManager(config=_default_config())
        mgr._arbitrage_llm_pid = 12345
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        mgr.stop_arbitrage_llm()
        assert mgr._arbitrage_llm_pid is None


class TestVRAMManagerStopCleanupLlmPorts:
    def test_stop_cleanup_llm_ports_kills_configured_port(self, monkeypatch):
        mgr = VRAMManager(config=_default_config(llm_cleanup_ports=[12345]))
        monkeypatch.setattr(VRAMManager, "_kill_port", lambda self, port: port == 12345)
        result = mgr.stop_cleanup_llm_ports()
        assert result is True

class TestEnsureArbitrageLlmHealthProbe:
    """Sonde de santé compatible modèles « reasoning » (incident du 11/06/2026).

    Un modèle reasoning dépense ses premiers tokens dans <think> (séparés en
    `reasoning_content` par llama.cpp) : avec 5 tokens et l'exigence `text` non vide,
    la sonde jugeait « malade » un serveur sain et le REDÉMARRAIT."""

    def _probe(self, monkeypatch, completion_payload):
        vm = VRAMManager(_default_config(arbitrage_api_model_id="modele-test"))
        restarted = {"called": False}
        monkeypatch.setattr(vm, "launch_arbitrage_llm", lambda: restarted.update(called=True) or True)
        monkeypatch.setattr(vm, "stop_arbitrage_llm", lambda: True)
        monkeypatch.setattr(vm, "_get_port_pid", lambda port: "1234")

        import requests as _requests

        class _Resp:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        monkeypatch.setattr(
            _requests, "get",
            lambda url, timeout=5: _Resp({"data": [{"id": "modele-test"}]}),
        )
        monkeypatch.setattr(
            _requests, "post",
            lambda url, json=None, timeout=60: _Resp(completion_payload),
        )
        result = vm.ensure_arbitrage_llm_ready(expected_model_id="modele-test")
        return result, restarted["called"]

    def test_reasoning_only_output_is_healthy(self, monkeypatch):
        """reasoning_content sans text = serveur VIVANT → réutilisé, pas redémarré."""
        ok, restarted = self._probe(monkeypatch, {
            "choices": [{"text": "", "reasoning_content": "Je réfléchis à la salutation."}],
        })
        assert ok is True
        assert restarted is False

    def test_plain_text_output_is_healthy(self, monkeypatch):
        ok, restarted = self._probe(monkeypatch, {
            "choices": [{"text": "Bonjour !"}],
        })
        assert ok is True
        assert restarted is False

    def test_empty_output_triggers_restart(self, monkeypatch):
        """Aucun token produit (ni texte ni raisonnement) = serveur réellement malade."""
        ok, restarted = self._probe(monkeypatch, {
            "choices": [{"text": "", "reasoning_content": ""}],
        })
        assert restarted is True
        assert ok is True  # relancée avec succès (mock)


class TestArbitrageLaunchLogFallback:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignore chmod 000 — le repli ne se déclenche pas")
    def test_unwritable_log_falls_back_to_per_user_path(self, monkeypatch, tmp_path):
        """/tmp sticky + fichier d'un autre utilisateur (EPERM) : la sortie du lancement
        doit être capturée AILLEURS, pas perdue (exit≠0 indiagnosticable sinon)."""
        blocked = tmp_path / "arbitrage.log"
        blocked.write_text("")
        blocked.chmod(0o000)
        script = tmp_path / "launch.sh"
        script.write_text("#!/bin/bash\necho bonjour-du-script\n")
        script.chmod(0o755)
        vm = VRAMManager(_default_config(
            arbitrage_script=str(script), arbitrage_log_path=str(blocked),
        ))
        monkeypatch.setattr(vm, "is_port_open", lambda port: False)
        monkeypatch.setattr(vm, "_wait_for_port", lambda port, timeout=600, proc=None, log_path=None: True)
        monkeypatch.setattr(vm, "_stop_cleanup_ports", lambda: None, raising=False)
        try:
            assert vm.launch_arbitrage_llm() is True
        finally:
            blocked.chmod(0o644)
        fallback = tmp_path / f"arbitrage.log.{os.getuid()}"
        time.sleep(0.3)  # le script écrit de façon asynchrone (Popen détaché)
        assert fallback.is_file()
        assert "bonjour-du-script" in fallback.read_text()

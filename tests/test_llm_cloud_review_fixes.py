"""Correctifs de la relecture du lot A cloud : admission, watchdog du runner, retries des phases."""
import shutil
import subprocess
from types import SimpleNamespace

from builders import make_config, make_job_stub
from test_opencode_runner import _FakePopen

from transcria.llm_tools.opencode_cli import OpencodeVersion
from transcria.llm_tools.opencode_runner import OpenCodeRunner
from transcria.queue.scheduler import QueueScheduler
from transcria.workflow.phases import correction as correction_phase
from transcria.workflow.phases import summary_llm as summary_phase

CLOUD = {"services": {"backend": "cloud"},
         "workflow": {"arbitration_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"}}}
TIMEOUT_ERR = "opencode interrompu (timeout absolu 7200s)"


def _boom(*a, **k):
    raise AssertionError("ne doit pas être appelé en cloud")


# ---------------------------------------------------------------- scheduler
class TestAdmissionCloud:
    def _scheduler(self, config):
        s = object.__new__(QueueScheduler)
        s.config = config
        s.allocator = SimpleNamespace(can_host_llm=_boom)
        s._vram_manager = _boom
        return s

    def test_cloud_admissible_sans_sonde(self):
        s = self._scheduler(make_config(CLOUD))
        assert s._llm_admissible({"phases": {"llm_arbitration": 60000}}, set()) is True

    def test_local_interroge_le_gestionnaire(self):
        s = self._scheduler(make_config())
        s._vram_manager = lambda: SimpleNamespace(is_arbitrage_llm_running=lambda: False)
        s.allocator = SimpleNamespace(can_host_llm=lambda mb: False)
        s._remote_phase_names = lambda: set()
        assert s._llm_admissible({"phases": {"llm_arbitration": 60000}}, set()) is False


# ---------------------------------------------------------------- runner
class TestRunnerErrorKind:
    def _run(self, tmp_path, monkeypatch, config, err):
        prompt = tmp_path / "p.txt"
        prompt.write_text("x")
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/opencode")
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakePopen(returncode=0))
        runner = OpenCodeRunner(str(tmp_path / "w"), model="mistral/mistral-large-4", config=config)
        monkeypatch.setattr(runner, "_preflight_error", lambda: None)
        monkeypatch.setattr(runner, "opencode_version", lambda path: OpencodeVersion(1, 19, 5), raising=False)
        monkeypatch.setattr(runner, "_communicate_with_watchdog", lambda proc, timeout: ("", "", err))
        return runner.run("t", str(prompt))

    def test_plafond_absolu_cloud(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, make_config(CLOUD), TIMEOUT_ERR)
        assert r["success"] is False and r["error_kind"] == "absolute_timeout"

    def test_gel_detecte_local(self, tmp_path, monkeypatch):
        r = self._run(tmp_path, monkeypatch, make_config(), "opencode interrompu (aucune sortie depuis 600s)")
        assert r["error_kind"] == "watchdog"


# ---------------------------------------------------------------- phases
class TestRetries:
    def _correction(self, config):
        calls = []

        def run_correction(*a, **k):
            calls.append(1)
            return {"success": False, "error": TIMEOUT_ERR, "error_kind": "absolute_timeout", "corrected_srt": ""}

        ocr = SimpleNamespace(run_correction=run_correction)
        correction_phase._invoke_correction_with_retries(
            ocr, make_job_stub(), staged_srt="a", staged_context="b", staged_lexicon="c",
            staged_invite=None, runner=None, config=config,
        )
        return len(calls)

    def test_correction_cloud_un_seul_essai(self):
        assert self._correction(make_config(CLOUD)) == 1

    def test_correction_local_trois_essais(self):
        assert self._correction(make_config()) == 3

    def _summary(self, config):
        calls = []

        def run_summary(*a, **k):
            calls.append(1)
            return {"_failure_kind": "opencode_error",
                    "_failure_detail": "opencode exit 1: timeout absolu 7200s"}

        runner = SimpleNamespace(
            _summary_usable=lambda parsed: False,
            vram=SimpleNamespace(ensure_arbitrage_llm_ready=lambda *a, **k: True),
        )
        sl = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                             error=lambda *a, **k: None)
        summary_phase._invoke_llm_with_retries(
            runner, SimpleNamespace(run_summary=run_summary), make_job_stub(), sl,
            staged_transcript="a", staged_context="b", staged_diar_ctx="c", staged_invite=None,
            prompt_subs={}, extract_keys=(), api_model_id=None, config=config,
        )
        return len(calls)

    def test_resume_cloud_un_seul_essai(self):
        assert self._summary(make_config(CLOUD)) == 1

    def test_resume_local_trois_essais(self):
        assert self._summary(make_config()) == 3

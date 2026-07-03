import pytest

from transcria.integrations.dashboard_client import DashboardClient
from transcria.gpu.opencode_runner import OpenCodeRunner



class TestDashboardClient:
    def test_instantiation(self):
        client = DashboardClient("http://127.0.0.1:5001")
        assert client.base_url == "http://127.0.0.1:5001"
        assert client.timeout == 3

    def test_custom_timeout(self):
        client = DashboardClient(timeout=5)
        assert client.timeout == 5

    def test_strips_trailing_slash(self):
        client = DashboardClient("http://127.0.0.1:5001/")
        assert client.base_url == "http://127.0.0.1:5001"

    def test_get_system_status_error_handling(self):
        client = DashboardClient("http://127.0.0.1:19999", timeout=1)
        status = client.get_system_status()
        assert isinstance(status, dict)
        assert status["available"] is False

    def test_get_system_status_short_circuits_when_dashboard_down(self):
        """Dashboard injoignable → un seul appel (metrics), pas 4 timeouts en série."""
        client = DashboardClient("http://127.0.0.1:5001", timeout=1)
        calls: list[str] = []

        def fake_get(path: str) -> dict:
            calls.append(path)
            return {"error": "connection refused", "available": False}

        client._get = fake_get  # type: ignore[method-assign]
        status = client.get_system_status()
        assert status["available"] is False
        assert calls == ["/api/v1/metrics"]  # court-circuit : aucun appel supplémentaire

    def test_get_system_status_available_when_metrics_ok(self):
        client = DashboardClient("http://127.0.0.1:5001", timeout=1)
        payloads = {
            "/api/v1/metrics": {"cpu": {"percent": 5}, "ram": {"percent": 30}, "model": "m"},
            "/api/v1/gpus": {"gpus": [{"index": 0}]},
            "/api/v1/services": {"services": {"llm": "up"}},
            "/api/v1/gpus/processes": {"processes": []},
        }
        client._get = lambda path: payloads[path]  # type: ignore[method-assign]
        status = client.get_system_status()
        assert status["available"] is True
        assert status["gpus"] == [{"index": 0}]
        assert status["model"] == "m"


class TestOpenCodeRunner:
    def test_run_summary_mentions_diarization_context(self, tmp_path, monkeypatch):
        transcript = tmp_path / "quick_transcript.txt"
        context = tmp_path / "job_context.yaml"
        diarization = tmp_path / "diarization_context.md"
        transcript.write_text("[0.0s -> 1.0s] Bonjour", encoding="utf-8")
        context.write_text("meeting: {}", encoding="utf-8")
        diarization.write_text("# Données de diarization acoustique", encoding="utf-8")

        captured = {}

        def fake_run(self, instruction, prompt_file, timeout=600):
            captured["instruction"] = instruction
            (tmp_path / "summary.md").write_text("# Résumé de contrôle\n\n## Synthèse\nOK", encoding="utf-8")
            return {"success": True, "output": "", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        parsed = OpenCodeRunner(str(tmp_path), model="local/test-llm-arbitrage").run_summary(
            str(transcript),
            str(context),
            str(diarization),
        )

        assert parsed["summary_text"].startswith("# Résumé de contrôle")
        assert str(diarization) in captured["instruction"]
        assert "diarization acoustique" in captured["instruction"]

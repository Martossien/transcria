import pytest

from transcria.integrations.dashboard_client import DashboardClient
from transcria.integrations.srt_editor_link import SrtEditorLink
from transcria.gpu.opencode_runner import OpenCodeRunner


class TestSrtEditorLink:
    def test_default_url(self):
        link = SrtEditorLink()
        assert link.link == "http://127.0.0.1:7861"

    def test_custom_url(self):
        link = SrtEditorLink("http://example.com:9000")
        assert link.link == "http://example.com:9000"

    def test_resolve_public_url_with_request_host(self):
        cfg = {"services": {"srt_editor_easy_url": "http://127.0.0.1:7861"}}
        url = SrtEditorLink.resolve_public_url(cfg, "55.153.230.50:7870")
        assert url == "http://55.153.230.50:7861"

    def test_resolve_public_url_localhost_only(self):
        cfg = {"services": {"srt_editor_easy_url": "http://127.0.0.1:7861"}}
        url = SrtEditorLink.resolve_public_url(cfg, "localhost:5000")
        assert url == "http://localhost:7861"

    def test_resolve_public_url_no_change_if_already_ip(self):
        cfg = {"services": {"srt_editor_easy_url": "http://192.168.1.5:7861"}}
        url = SrtEditorLink.resolve_public_url(cfg, "55.153.230.50:7870")
        assert url == "http://192.168.1.5:7861"

    def test_get_server_url_from_config(self):
        cfg = {"services": {"srt_editor_easy_url": "http://10.0.0.1:8000"}}
        url = SrtEditorLink.get_server_url(cfg)
        assert url == "http://10.0.0.1:8000"

    def test_get_server_url_default(self):
        cfg = {}
        url = SrtEditorLink.get_server_url(cfg)
        assert url == "http://127.0.0.1:7861"


class TestDashboardClient:
    def test_instantiation(self):
        client = DashboardClient("http://127.0.0.1:5001")
        assert client.base_url == "http://127.0.0.1:5001"
        assert client.timeout == 10

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
        assert not status.get("available", True) or "error" in status


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

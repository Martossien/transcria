
from transcria.gpu.opencode_runner import OpenCodeRunner


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


class TestSystemStatusLocal:
    """C2.3 — la page Système lit des sources LOCALES (llmdashboard retiré)."""

    def test_forme_du_contrat(self):
        from transcria.diagnostics.system_status import get_system_status
        status = get_system_status()
        for key in ("cpu", "ram", "gpus", "services", "available"):
            assert key in status
        assert isinstance(status["gpus"], list)

    def test_cpu_ram_locaux_disponibles(self):
        # psutil est une dépendance du projet : cpu/ram doivent être renseignés.
        from transcria.diagnostics.system_status import get_system_status
        status = get_system_status()
        assert status["available"] is True
        assert status["ram"].get("total", 0) > 0

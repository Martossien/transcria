"""Tests for SummaryGenerator — generate_quick_summary and _llm_summarize paths."""
import json
import pytest

from transcria.stt.summary import SummaryGenerator


def _default_cfg(tmp_path):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"summary_llm": {"enabled": False}},
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }


class TestSummaryGeneratorLlmSummarize:
    def test_llm_summarize_success(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"
        cfg["workflow"]["summary_llm"]["model_id"] = "test-model"
        cfg["workflow"]["summary_llm"]["timeout_seconds"] = 30

        gen = SummaryGenerator(cfg)

        fake_response = type("R", (), {
            "status_code": 200,
            "json": lambda self: {
                "choices": [{"message": {"content": "Résumé : La réunion a porté sur le budget Q1."}}],
            },
            "raise_for_status": lambda self: None,
        })()

        monkeypatch.setattr(requests, "post", lambda *a, **kw: fake_response)

        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "summary_prompt.txt").write_text("Tu es un assistant.", encoding="utf-8")

        import transcria.stt.summary as summary_module
        original_dir = summary_module.Path
        monkeypatch.setattr(summary_module, "Path", lambda p: original_dir(p) if not str(p).endswith("summary_prompt.txt") else prompt_dir / "summary_prompt.txt")

        result = gen._llm_summarize("Transcription de test longue", None)
        assert "budget Q1" in result
        assert len(result) > 20

    def test_llm_summarize_too_short_response(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"

        gen = SummaryGenerator(cfg)

        fake_response = type("R", (), {
            "status_code": 200,
            "json": lambda self: {
                "choices": [{"message": {"content": "OK"}}],
            },
            "raise_for_status": lambda self: None,
        })()

        monkeypatch.setattr(requests, "post", lambda *a, **kw: fake_response)

        result = gen._llm_summarize("Transcription test", None)
        assert "indisponible" in result.lower() or "trop courte" in result.lower()

    def test_llm_summarize_connection_error(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"

        gen = SummaryGenerator(cfg)

        monkeypatch.setattr(requests, "post", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError("refused")))

        result = gen._llm_summarize("Transcription test", None)
        assert "indisponible" in result.lower() or "erreur" in result.lower()

    def test_llm_summarize_http_error(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"

        gen = SummaryGenerator(cfg)

        def raise_http(*a, **kw):
            raise requests.HTTPError("500 Server Error")

        fake_response = type("R", (), {
            "status_code": 500,
            "raise_for_status": lambda self: (_ for _ in ()).throw(requests.HTTPError("500")),
        })()

        monkeypatch.setattr(requests, "post", lambda *a, **kw: (_ for _ in ()).throw(requests.HTTPError("500")))

        result = gen._llm_summarize("Transcription test", None)
        assert "indisponible" in result.lower() or "erreur" in result.lower()

    def test_llm_summarize_empty_response(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"

        gen = SummaryGenerator(cfg)

        fake_response = type("R", (), {
            "status_code": 200,
            "json": lambda self: {"choices": [{"message": {"content": ""}}]},
            "raise_for_status": lambda self: None,
        })()

        monkeypatch.setattr(requests, "post", lambda *a, **kw: fake_response)

        result = gen._llm_summarize("Transcription test", None)
        assert "indisponible" in result.lower() or "trop courte" in result.lower()

    def test_llm_summarize_timeout(self, tmp_path, monkeypatch):
        import requests

        cfg = _default_cfg(tmp_path)
        cfg["workflow"]["summary_llm"]["enabled"] = True
        cfg["workflow"]["summary_llm"]["api_base"] = "http://127.0.0.1:8080/v1"

        gen = SummaryGenerator(cfg)

        monkeypatch.setattr(requests, "post", lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("timed out")))

        result = gen._llm_summarize("Transcription test", None)
        assert "indisponible" in result.lower() or "erreur" in result.lower()


class TestSummaryGeneratorGenerateQuickSummary:
    def test_generate_quick_summary_saves_files_and_returns(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            cfg = _default_cfg(tmp_path)
            from transcria.jobs.store import JobStore
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.cohere_transcriber import CohereTranscriber

            job = JobStore.create_job(owner_id, "Quick Summary")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

            fake_segments = [
                {"start": 0.0, "end": 5.0, "text": "Bonjour à tous"},
                {"start": 5.0, "end": 10.0, "text": "Le budget est bouclé"},
            ]

            monkeypatch.setattr(CohereTranscriber, "load", lambda self: True)
            monkeypatch.setattr(CohereTranscriber, "transcribe", lambda self, *a, **kw: fake_segments)
            monkeypatch.setattr(CohereTranscriber, "offload", lambda self: None)

            gen = SummaryGenerator(cfg)
            audio_path = tmp_path / "test_audio.wav"
            audio_path.write_text("fake audio")

            result = gen.generate_quick_summary(job, audio_path, gpu_index=0)

            assert "transcript_text" in result
            assert "Bonjour à tous" in result["transcript_text"]
            assert result["segment_count"] == 2

            transcript_file = fs.load_text("summary/quick_transcript.txt")
            assert transcript_file is not None
            assert "Bonjour à tous" in transcript_file

            summary_json = fs.load_json("summary/summary.json")
            assert summary_json is not None
            assert len(summary_json["segments"]) == 2

            summary_md = fs.load_text("summary/summary.md")
            assert summary_md is not None
            assert "Résumé" in summary_md or "Extrait" in summary_md
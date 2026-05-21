"""Tests PipelineService : intégration AudioSceneAnalyzer + SourceSeparationService."""
import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc(config=None):
    """Instancie PipelineService sans déclencher __init__ (pas de WorkflowRunner)."""
    from transcria.services.pipeline_service import PipelineService
    svc = PipelineService.__new__(PipelineService)
    svc.config = config or {}
    svc.runner = MagicMock()
    return svc


def _job(job_id="test-job-001"):
    j = MagicMock()
    j.id = job_id
    return j


# ---------------------------------------------------------------------------
# _run_audio_scene_analysis
# ---------------------------------------------------------------------------


class TestPipelineAudioSceneAnalysis:
    """Analyse de scène pré-transcription : désactivée / indisponible / exception / succès."""

    def _cfg_disabled(self, tmp_path):
        return {
            "workflow": {"audio_scene": {"enabled": False}},
            "storage": {"jobs_dir": str(tmp_path)},
        }

    def _cfg_enabled(self, tmp_path):
        return {
            "workflow": {"audio_scene": {"enabled": True, "timeout_s": 30}},
            "storage": {"jobs_dir": str(tmp_path)},
        }

    def test_disabled_returns_empty_dict(self, tmp_path):
        svc = _make_svc(self._cfg_disabled(tmp_path))
        result = svc._run_audio_scene_analysis(_job(), str(tmp_path / "audio.wav"), MagicMock())
        assert result == {}

    def test_unavailable_returns_empty_dict(self, tmp_path, monkeypatch):
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer
        monkeypatch.setattr(AudioSceneAnalyzer, "available", property(lambda self: False))

        svc = _make_svc(self._cfg_enabled(tmp_path))
        result = svc._run_audio_scene_analysis(_job(), str(tmp_path / "audio.wav"), MagicMock())
        assert result == {}

    def test_analyze_exception_returns_empty_dict(self, tmp_path, monkeypatch):
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer
        monkeypatch.setattr(AudioSceneAnalyzer, "available", property(lambda self: True))
        monkeypatch.setattr(
            AudioSceneAnalyzer, "analyze",
            lambda self, p: (_ for _ in ()).throw(RuntimeError("crash worker")),
        )
        svc = _make_svc(self._cfg_enabled(tmp_path))
        result = svc._run_audio_scene_analysis(_job(), str(tmp_path / "audio.wav"), MagicMock())
        assert result == {}

    def test_success_returns_scene_and_saves_json(self, tmp_path, monkeypatch):
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer
        from transcria.jobs.filesystem import JobFilesystem

        scene = {"has_music": False, "has_noise": True, "speech_ratio": 0.87}
        monkeypatch.setattr(AudioSceneAnalyzer, "available", property(lambda self: True))
        monkeypatch.setattr(AudioSceneAnalyzer, "analyze", lambda self, p: scene)

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem, "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc(self._cfg_enabled(tmp_path))
        result = svc._run_audio_scene_analysis(_job(), str(tmp_path / "audio.wav"), MagicMock())

        assert result == scene
        assert saved.get("path") == "metadata/audio_scene.json"
        assert saved.get("data") == scene

    def test_refresh_audio_quality_with_scene_saves_enriched_decision(self, tmp_path, monkeypatch):
        from transcria.jobs.filesystem import JobFilesystem

        cfg = {
            "workflow": {
                "audio_quality": {
                    "scene_affects_quality_score": False,
                    "max_scene_noise_ratio": 0.20,
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        }
        scene = {
            "has_noise": True,
            "speech_ratio": 0.7,
            "noise_ratio": 0.25,
            "problem_segments": [{"label": "noise"}],
        }

        def fake_load_json(self, path):
            if path == "summary/summary.json":
                return {"diagnostics": {"level": "ok"}}
            if path == "metadata/audio_analysis.json":
                return {"duration_seconds": 180}
            return {}

        saved: dict = {}
        monkeypatch.setattr(JobFilesystem, "load_json", fake_load_json)
        monkeypatch.setattr(
            JobFilesystem, "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc(cfg)
        svc._refresh_audio_quality_with_scene(_job(), scene, MagicMock())

        assert saved["path"] == "metadata/audio_quality_decision.json"
        assert saved["data"]["level"] == "ok"
        assert "scene_bruit_important" in saved["data"]["scene_findings"]
        assert saved["data"]["scene_metrics"]["noise_ratio"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# _run_source_separation
# ---------------------------------------------------------------------------


class TestPipelineSourceSeparation:
    """Décision + séparation : refus / succès / dégradation gracieuse."""

    def _cfg(self, tmp_path):
        return {
            "workflow": {
                "source_separation": {
                    "enabled": True,
                    "decision": {"min_score": 2, "min_duration_s": 60},
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        }

    def test_decider_says_no_returns_original_path(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationDecider
        from transcria.jobs.filesystem import JobFilesystem

        monkeypatch.setattr(JobFilesystem, "load_json", lambda self, p: None)
        monkeypatch.setattr(
            SourceSeparationDecider, "should_separate",
            lambda self, *a, **kw: (False, ["score_insuffisant"]),
        )
        svc = _make_svc(self._cfg(tmp_path))
        audio = str(tmp_path / "audio.wav")
        result = svc._run_source_separation(_job(), audio, {}, MagicMock())
        assert result == audio

    def test_decider_says_yes_service_succeeds_returns_vocals(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationDecider, SourceSeparationService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.touch()
        vocals = tmp_path / "vocals.wav"
        vocals.touch()

        monkeypatch.setattr(JobFilesystem, "load_json", lambda self, p: None)
        monkeypatch.setattr(
            SourceSeparationDecider, "should_separate",
            lambda self, *a, **kw: (True, ["music_detected"]),
        )
        monkeypatch.setattr(SourceSeparationService, "separate", lambda self, src, dst: vocals)

        svc = _make_svc(self._cfg(tmp_path))
        result = svc._run_source_separation(_job(), str(audio), {"has_music": True}, MagicMock())
        assert result == str(vocals)

    def test_decider_says_yes_service_fails_returns_original(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationDecider, SourceSeparationService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.touch()

        monkeypatch.setattr(JobFilesystem, "load_json", lambda self, p: None)
        monkeypatch.setattr(
            SourceSeparationDecider, "should_separate",
            lambda self, *a, **kw: (True, ["music_detected"]),
        )
        # Dégradation gracieuse : service retourne le chemin source
        monkeypatch.setattr(SourceSeparationService, "separate", lambda self, src, dst: src)

        svc = _make_svc(self._cfg(tmp_path))
        result = svc._run_source_separation(_job(), str(audio), {"has_music": True}, MagicMock())
        assert result == str(audio)


# ---------------------------------------------------------------------------
# _run_audio_scene_filter
# ---------------------------------------------------------------------------


class TestPipelineAudioSceneFilter:
    """Filtrage scène : refus, succès et métadonnées d'audit."""

    def _cfg(self, tmp_path):
        return {
            "workflow": {
                "audio_scene_filter": {
                    "enabled": True,
                    "enabled_for_modes": ["quality"],
                    "target_labels": ["noise"],
                    "min_segment_s": 2.0,
                    "min_total_muted_s": 2.0,
                    "edge_keep_s": 0.0,
                    "max_intervals": 10,
                    "timeout_s": 30,
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        }

    def test_disabled_filter_returns_original_path(self, tmp_path):
        svc = _make_svc({"workflow": {"audio_scene_filter": {"enabled": False}}})
        audio = str(tmp_path / "audio.wav")

        result = svc._run_audio_scene_filter(_job(), audio, "quality", {}, MagicMock())

        assert result == audio

    def test_success_saves_filter_metadata_and_returns_filtered_path(self, tmp_path, monkeypatch):
        from transcria.audio.scene_filter import AudioSceneFilterService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        intervals = [{"label": "noise", "start": 1.0, "end": 4.0, "duration_s": 3.0}]
        scene = {"problem_segments": intervals}

        monkeypatch.setattr(
            AudioSceneFilterService,
            "apply",
            lambda self, input_path, output_path, intervals: output_path,
        )

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem, "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc(self._cfg(tmp_path))
        result = svc._run_audio_scene_filter(_job(), str(audio), "quality", scene, MagicMock())

        assert result.endswith("scene_filtered.wav")
        assert saved["path"] == "metadata/audio_scene_filter.json"
        assert saved["data"]["preserve_timeline"] is True
        assert saved["data"]["intervals"] == intervals


# ---------------------------------------------------------------------------
# Ordre d'exécution dans _run_pipeline_steps
# ---------------------------------------------------------------------------


class TestPipelineStepsOrder:
    """scene_analysis et source_sep doivent être appelés AVANT la transcription."""

    def test_pre_transcription_steps_run_before_transcription(self, monkeypatch):
        import transcria.services.pipeline_service as ps_mod

        monkeypatch.setattr(ps_mod, "JobStore", MagicMock())

        svc = _make_svc({})
        call_order: list = []

        monkeypatch.setattr(svc, "_is_cancel_requested", lambda *a: False)
        monkeypatch.setattr(svc, "_config_for_mode", lambda *a: {})
        monkeypatch.setattr(
            svc, "_run_audio_scene_analysis",
            lambda *a: call_order.append("scene") or {},
        )
        monkeypatch.setattr(
            svc, "_run_source_separation",
            lambda *a: call_order.append("sep") or str(a[1]),
        )
        monkeypatch.setattr(
            svc, "_run_audio_scene_filter",
            lambda *a: call_order.append("filter") or str(a[1]),
        )

        def fake_transcribe(*a):
            call_order.append("transcription")
            return {"segments": []}

        svc.runner.run_transcription.side_effect = fake_transcribe
        monkeypatch.setattr(svc, "_define_pipeline_steps", lambda *a: [])

        svc._run_pipeline_steps(_job(), "/fake/audio.wav", "fast", MagicMock())

        assert call_order.index("scene") < call_order.index("transcription")
        assert call_order.index("sep") < call_order.index("transcription")
        assert call_order.index("filter") < call_order.index("transcription")

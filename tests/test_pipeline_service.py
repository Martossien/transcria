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
# _run_audio_preflight
# ---------------------------------------------------------------------------


class TestPipelineAudioPreflight:
    """Pré-diagnostic audio : désactivé, succès et sauvegarde JSON."""

    def test_disabled_returns_empty_dict(self, tmp_path):
        svc = _make_svc({
            "workflow": {"audio_preflight": {"enabled": False}},
            "storage": {"jobs_dir": str(tmp_path)},
        })

        result = svc._run_audio_preflight(_job(), str(tmp_path / "audio.wav"), MagicMock())

        assert result == {}

    def test_success_saves_preflight_json(self, tmp_path, monkeypatch):
        from transcria.audio.preflight import AudioPreflightAnalyzer
        from transcria.jobs.filesystem import JobFilesystem

        preflight = {
            "rms": 0.006,
            "peak": 0.1,
            "estimated_snr_db": 4.0,
            "bandwidth_95_hz": 3200.0,
            "risk_level": "degrade",
            "flags": ["audio_tres_faible", "risque_transcription_non_fiable"],
        }
        monkeypatch.setattr(AudioPreflightAnalyzer, "analyze", lambda self, p: preflight)

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem,
            "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc({
            "workflow": {"audio_preflight": {"enabled": True}},
            "storage": {"jobs_dir": str(tmp_path)},
        })
        result = svc._run_audio_preflight(_job(), str(tmp_path / "audio.wav"), MagicMock())

        assert result == preflight
        assert saved["path"] == "metadata/audio_preflight.json"
        assert saved["data"] == preflight


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

    def test_disabled_returns_original_without_decider(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationDecider

        called = False

        def fake_should_separate(self, *args, **kwargs):
            nonlocal called
            called = True
            return True, ["should_not_run"]

        monkeypatch.setattr(SourceSeparationDecider, "should_separate", fake_should_separate)

        cfg = self._cfg(tmp_path)
        cfg["workflow"]["source_separation"]["enabled"] = False
        svc = _make_svc(cfg)
        audio = str(tmp_path / "audio.wav")

        result = svc._run_source_separation(_job(), audio, {}, MagicMock())

        assert result == audio
        assert called is False

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
# _run_audio_normalization
# ---------------------------------------------------------------------------


class TestPipelineAudioNormalization:
    """Normalisation pré-STT : refus, succès et métadonnées d'audit."""

    def _cfg(self, tmp_path):
        return {
            "workflow": {
                "audio_normalization": {
                    "enabled": True,
                    "enabled_for_modes": ["quality"],
                    "loudnorm_enabled": True,
                    "target_i": -23.0,
                    "true_peak": -2.0,
                    "lra": 11.0,
                    "highpass_hz": 80,
                    "timeout_s": 30,
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        }

    def test_disabled_normalization_returns_original_path(self, tmp_path):
        svc = _make_svc({"workflow": {"audio_normalization": {"enabled": False}}})
        audio = str(tmp_path / "audio.wav")

        result = svc._run_audio_normalization(_job(), audio, "quality", MagicMock())

        assert result == audio

    def test_success_saves_normalization_metadata_and_returns_normalized_path(self, tmp_path, monkeypatch):
        from transcria.audio.normalization import AudioNormalizationService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        filters = ["highpass=f=80", "loudnorm=I=-23:TP=-2:LRA=11"]

        monkeypatch.setattr(
            AudioNormalizationService,
            "apply",
            lambda self, input_path, output_path, filters: output_path,
        )

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem, "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc(self._cfg(tmp_path))
        result = svc._run_audio_normalization(_job(), str(audio), "quality", MagicMock())

        assert result.endswith("normalized.wav")
        assert saved["path"] == "metadata/audio_normalization.json"
        assert saved["data"]["preserve_timeline"] is True
        assert saved["data"]["filters"] == filters

    def test_weak_voice_profile_uses_preflight_and_saves_metadata(self, tmp_path, monkeypatch):
        from transcria.audio.normalization import AudioNormalizationService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        monkeypatch.setattr(
            AudioNormalizationService,
            "apply",
            lambda self, input_path, output_path, filters: output_path,
        )

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem,
            "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        cfg = {
            "workflow": {
                "audio_normalization": {
                    "enabled": False,
                    "weak_voice": {"enabled": True, "target_rms": 0.05, "max_gain": 8.0},
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        }
        svc = _make_svc(cfg)
        result = svc._run_audio_normalization(
            _job(), str(audio), "quality", MagicMock(),
            {"rms": 0.006, "flags": ["audio_tres_faible"]},
        )

        assert result.endswith("normalized.wav")
        assert saved["path"] == "metadata/audio_normalization.json"
        assert saved["data"]["forced"] is True
        assert "audio_faible_preflight" in saved["data"]["reasons"]


# ---------------------------------------------------------------------------
# _run_audio_denoise
# ---------------------------------------------------------------------------


class TestPipelineAudioDenoise:
    """Débruitage expérimental : refus et métadonnées d'audit."""

    def test_disabled_denoise_returns_original_path(self, tmp_path):
        svc = _make_svc({"workflow": {"audio_denoise": {"enabled": False}}})
        audio = str(tmp_path / "audio.wav")

        result = svc._run_audio_denoise(_job(), audio, "quality", {}, MagicMock())

        assert result == audio

    def test_success_saves_denoise_metadata_and_returns_denoised_path(self, tmp_path, monkeypatch):
        from transcria.audio.denoise import AudioDenoiseService
        from transcria.jobs.filesystem import JobFilesystem

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"audio")
        monkeypatch.setattr(
            AudioDenoiseService,
            "apply",
            lambda self, input_path, output_path, filters: output_path,
        )

        saved: dict = {}
        monkeypatch.setattr(
            JobFilesystem,
            "save_json",
            lambda self, path, data: saved.update({"path": path, "data": data}),
        )

        svc = _make_svc({
            "workflow": {
                "audio_denoise": {
                    "enabled": True,
                    "enabled_for_modes": ["quality"],
                    "backend": "ffmpeg_afftdn",
                    "trigger_flags": ["snr_faible"],
                }
            },
            "storage": {"jobs_dir": str(tmp_path)},
        })
        result = svc._run_audio_denoise(_job(), str(audio), "quality", {"flags": ["snr_faible"]}, MagicMock())

        assert result.endswith("denoised.wav")
        assert saved["path"] == "metadata/audio_denoise.json"
        assert saved["data"]["preserve_timeline"] is True
        assert saved["data"]["experimental"] is True


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
            svc,
            "_run_audio_preflight",
            lambda *a: call_order.append("preflight") or {},
        )
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
        monkeypatch.setattr(
            svc, "_run_audio_denoise",
            lambda *a: call_order.append("denoise") or str(a[1]),
        )
        monkeypatch.setattr(
            svc, "_run_audio_normalization",
            lambda *a: call_order.append("normalization") or str(a[1]),
        )

        def fake_transcribe(*a):
            call_order.append("transcription")
            return {"segments": []}

        svc.runner.run_transcription.side_effect = fake_transcribe
        monkeypatch.setattr(svc, "_define_pipeline_steps", lambda *a: [])

        svc._run_pipeline_steps(_job(), "/fake/audio.wav", "fast", MagicMock())

        assert call_order.index("preflight") < call_order.index("transcription")
        assert call_order.index("scene") < call_order.index("transcription")
        assert call_order.index("sep") < call_order.index("transcription")
        assert call_order.index("filter") < call_order.index("transcription")
        assert call_order.index("denoise") < call_order.index("transcription")
        assert call_order.index("normalization") < call_order.index("transcription")


class TestPipelineSteps:
    """La phase de relecture finale s'insère après correction, avant qualité."""

    def test_final_review_between_correction_and_quality(self):
        svc = _make_svc({"workflow": {"enable_quality_mode": True}})
        names = [s["name"] for s in svc._define_pipeline_steps(_job(), "a.wav", "quality")]
        assert names == ["diarization", "correction", "final_review", "quality", "export"]

    def test_no_final_review_when_arbitration_disabled(self):
        svc = _make_svc({"workflow": {"arbitration_llm": {"enabled": False}}})
        names = [s["name"] for s in svc._define_pipeline_steps(_job(), "a.wav", "fast")]
        assert "final_review" not in names
        assert "correction" not in names
        assert names == ["quality", "export"]


class TestStepProgressConsistency:
    """Les % du wrapper _publish_step_progress concordent avec ceux des méthodes."""

    def test_final_review_and_quality_percents(self):
        svc = _make_svc()
        captured = {}

        def fake_update(job_id, **kw):
            captured.setdefault(kw.get("phase"), []).append(kw.get("percent"))

        svc._progress = MagicMock(); svc._progress.update = fake_update
        for name in ("final_review", "quality"):
            svc._publish_step_progress(_job(), name, starting=True)
            svc._publish_step_progress(_job(), name, starting=False)
        # final_review : 83 (start) → 89 (end) ; quality : 90 → 92 (décalés pour la phase)
        assert captured["final_review"] == [83, 89]
        assert captured["quality"] == [90, 92]

    def test_unknown_step_falls_back_without_percent(self):
        svc = _make_svc()
        captured = {}
        svc._progress = MagicMock()
        svc._progress.update = lambda job_id, **kw: captured.setdefault(kw.get("phase"), []).append(kw.get("percent"))
        svc._publish_step_progress(_job(), "inconnue", starting=True)
        assert captured["inconnue"] == [None]

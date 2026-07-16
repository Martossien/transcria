"""Tests PipelineService : intégration AudioSceneAnalyzer + SourceSeparationService."""
from unittest.mock import MagicMock

import pytest

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


class TestPipelineStepsByProfile:
    """Phase 4 : sélection des étapes machine pilotée par le profil de traitement."""

    def _names(self, profile_id, config=None):
        from transcria.workflow.profiles import get_profile
        svc = _make_svc(config or {"workflow": {"enable_quality_mode": True}})
        return [s["name"] for s in svc._define_pipeline_steps_for_profile(_job(), "a.wav", get_profile(profile_id))]

    def test_srt_express_ni_diarisation_ni_correction(self):
        assert self._names("srt_express") == ["quality", "export"]

    def test_srt_locuteurs_pas_de_phase_diarisation(self):
        # Spike : locuteurs via détection wizard, pas la phase diarisation du pipeline.
        assert self._names("srt_locuteurs") == ["quality", "export"]

    def test_word_structure_diarise_sans_corriger(self):
        assert self._names("word_structure") == ["diarization", "quality", "export"]

    def test_word_corrige_chaine_complete_de_correction(self):
        assert self._names("word_corrige") == ["diarization", "correction", "final_review", "quality", "export"]

    def test_dossier_qualite_reproduit_le_workflow_quality(self):
        # Golden : doit être identique à l'ancien mode `quality`.
        assert self._names("dossier_qualite") == ["diarization", "correction", "final_review", "quality", "export"]

    def test_legacy_fast_reproduit_le_workflow_fast(self):
        # Golden : ancien `fast` = correction + relecture, SANS diarisation.
        assert self._names("legacy_fast") == ["correction", "final_review", "quality", "export"]

    def test_diarisation_respecte_enable_quality_mode(self):
        # Parité : enable_quality_mode=False supprime la diarisation même pour un profil qui diarise.
        assert self._names("word_structure", {"workflow": {"enable_quality_mode": False}}) == ["quality", "export"]


class TestResolveProfile:
    """Phase 4 : le profil persisté (Phase 2) prime sur le mode legacy."""

    def _job_with_profile(self, pid):
        from unittest.mock import MagicMock
        j = MagicMock()
        j.get_extra_data.return_value = {"execution": {"processing_profile_id": pid}}
        return j

    def test_profil_persiste_prioritaire(self):
        svc = _make_svc({})
        # mode legacy "fast" mais profil persisté word_corrige → word_corrige gagne.
        profile = svc._resolve_profile(self._job_with_profile("word_corrige"), "fast")
        assert profile.id == "word_corrige"

    def test_repli_sur_mode_si_pas_de_profil_persiste(self):
        svc = _make_svc({})
        profile = svc._resolve_profile(self._job_with_profile(None), "quality")
        assert profile.id == "dossier_qualite"

    def test_profil_persiste_inconnu_ignore(self):
        svc = _make_svc({})
        profile = svc._resolve_profile(self._job_with_profile("inexistant"), "fast")
        assert profile.id == "legacy_fast"


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


# ---------------------------------------------------------------------------
# estimate_profile_resources — profil VRAM d'admission par profil (Phase 3)
# ---------------------------------------------------------------------------


class TestEstimateProfileResources:
    """Le profil VRAM ne réserve QUE les phases réellement exécutées par le profil."""

    _CFG = {
        "models": {"stt_backend": "cohere", "diarization_backend": "pyannote"},
        "gpu": {"llm_vram_mb": 60000},
        "workflow": {"arbitration_llm": {"enabled": True}},
    }

    def _estimate(self, profile_id, cfg=None):
        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow.profiles import get_profile
        return PipelineService.estimate_profile_resources(cfg or self._CFG, get_profile(profile_id))

    def test_srt_express_stt_seul_aucune_llm_ni_diarisation(self):
        res = self._estimate("srt_express")
        assert set(res["phases"]) == {"stt"}
        assert res["llm_shared"] is False
        assert res["processing_profile_id"] == "srt_express"
        assert res["mode"] == "fast"

    def test_srt_locuteurs_pas_de_phase_diarisation(self):
        # Spike : les locuteurs viennent de la détection wizard, pas de la phase diarisation.
        res = self._estimate("srt_locuteurs")
        assert set(res["phases"]) == {"stt"}

    def test_word_rapide_llm_sans_diarisation(self):
        res = self._estimate("word_rapide")
        assert set(res["phases"]) == {"stt", "llm_arbitration"}
        assert res["llm_shared"] is True

    def test_dossier_qualite_les_trois_phases(self):
        res = self._estimate("dossier_qualite")
        assert set(res["phases"]) == {"stt", "diarization", "llm_arbitration"}
        assert res["mode"] == "quality"

    def test_llm_non_reservee_si_arbitrage_desactive(self):
        cfg = {**self._CFG, "workflow": {"arbitration_llm": {"enabled": False}}}
        res = self._estimate("word_rapide", cfg)
        assert "llm_arbitration" not in res["phases"]

    def test_estimate_job_vram_delegue_compat_fast_quality(self):
        from transcria.services.pipeline_service import PipelineService
        fast = PipelineService.estimate_job_vram(self._CFG, "fast")
        assert "diarization" not in fast["phases"]
        quality = PipelineService.estimate_job_vram(self._CFG, "quality")
        assert "diarization" in quality["phases"]


class TestTypeFieldsGating:
    """Micro-étape « champs du type » (trou macro) : insérée SEULEMENT si le profil ne
    fait pas de relecture finale ET qu'un type perso avec extract_fields est choisi."""

    def _steps(self, profile_id, has_type_fields):
        from unittest.mock import patch

        from transcria.workflow.profiles import get_profile
        svc = _make_svc({"workflow": {"enable_quality_mode": True, "arbitration_llm": {"enabled": True}}})
        job = _job()
        with patch.object(svc, "_job_has_type_extract_fields", return_value=has_type_fields):
            steps = svc._define_pipeline_steps_for_profile(job, "/audio.wav", get_profile(profile_id))
        return [s["name"] for s in steps]

    def test_word_structure_avec_type_insere_letape(self):
        # Word structuré (pas de relecture finale) + type avec extract_fields → micro-étape
        names = self._steps("word_structure", has_type_fields=True)
        assert "type_fields" in names
        assert "final_review" not in names          # ce profil ne fait pas la relecture

    def test_word_structure_sans_type_pas_detape(self):
        names = self._steps("word_structure", has_type_fields=False)
        assert "type_fields" not in names           # aucun coût quand pas de type

    def test_word_corrige_jamais_de_microetape(self):
        # Word corrigé fait déjà la relecture finale (qui extrait les champs) → pas de doublon
        names = self._steps("word_corrige", has_type_fields=True)
        assert "type_fields" not in names
        assert "final_review" in names

    def test_srt_express_pas_de_microetape_sans_resume(self):
        # srt_express ne fait pas de résumé (requires_summary=False) → jamais de micro-étape,
        # même si on force has_type_fields (un tel job ne peut de toute façon pas choisir de type)
        names = self._steps("srt_express", has_type_fields=True)
        assert "type_fields" not in names


# ---------------------------------------------------------------------------
# _inject_granite_lexicon_keywords
# ---------------------------------------------------------------------------


class TestInjectGraniteLexiconKeywords:
    """Injection du lexique de session dans le prompt Granite « Keywords: »."""

    def _cfg(self, tmp_path, enabled=True, backend="granite"):
        return {
            "models": {"stt_backend": backend},
            "storage": {"jobs_dir": str(tmp_path)},
            "granite": {
                "prompt_mode": "asr_punctuated",
                "keywords": [],
                "lexicon_keywords": {
                    "enabled": enabled,
                    "priorities": ["critique", "importante"],
                    "max_terms": 50,
                },
            },
        }

    def _write_lexicon(self, tmp_path, job_id, entries):
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(str(tmp_path), job_id)
        fs.save_json("context/session_lexicon.json", entries)
        return fs

    def test_noop_si_backend_non_granite(self, tmp_path):
        svc = _make_svc()
        cfg = self._cfg(tmp_path, backend="cohere")
        job = _job()
        self._write_lexicon(tmp_path, job.id, [{"term": "DRITE", "priority": "critique"}])

        svc._inject_granite_lexicon_keywords(cfg, job)

        assert cfg["granite"]["keywords"] == []
        assert cfg["granite"]["prompt_mode"] == "asr_punctuated"

    def test_noop_si_desactive(self, tmp_path):
        svc = _make_svc()
        cfg = self._cfg(tmp_path, enabled=False)
        job = _job()
        self._write_lexicon(tmp_path, job.id, [{"term": "DRITE", "priority": "critique"}])

        svc._inject_granite_lexicon_keywords(cfg, job)

        assert cfg["granite"]["keywords"] == []
        assert cfg["granite"]["prompt_mode"] == "asr_punctuated"

    def test_injecte_termes_et_bascule_prompt_mode(self, tmp_path):
        svc = _make_svc()
        cfg = self._cfg(tmp_path)
        job = _job()
        fs = self._write_lexicon(
            tmp_path,
            job.id,
            [
                {"term": "DRITE", "priority": "critique"},
                {"term": "quorum", "replace_by": "quorum", "priority": "importante"},
                {"term": "banal", "priority": "normale"},  # hors priorités retenues
            ],
        )

        svc._inject_granite_lexicon_keywords(cfg, job)

        assert cfg["granite"]["prompt_mode"] == "keywords"
        assert "DRITE" in cfg["granite"]["keywords"]
        assert "quorum" in cfg["granite"]["keywords"]
        assert "banal" not in cfg["granite"]["keywords"]
        stats = fs.load_json("metadata/granite_keywords.json")
        assert stats["injected_terms"] == 2
        assert stats["prompt_mode"] == "keywords"

    def test_lexique_vide_ne_change_rien(self, tmp_path):
        svc = _make_svc()
        cfg = self._cfg(tmp_path)
        job = _job()
        self._write_lexicon(tmp_path, job.id, [])

        svc._inject_granite_lexicon_keywords(cfg, job)

        assert cfg["granite"]["keywords"] == []
        assert cfg["granite"]["prompt_mode"] == "asr_punctuated"

    def test_lexique_absent_ne_change_rien(self, tmp_path):
        svc = _make_svc()
        cfg = self._cfg(tmp_path)

        svc._inject_granite_lexicon_keywords(cfg, _job())

        assert cfg["granite"]["keywords"] == []
        assert cfg["granite"]["prompt_mode"] == "asr_punctuated"


# ---------------------------------------------------------------------------
# Branches des modules d'étapes audio (B2 lot 1) — chemins forcés et replis
# ---------------------------------------------------------------------------


class TestNormalizationForcedPaths:
    """Chemins forcés de la normalisation : loudnorm auto (silence) et replis."""

    def _svc(self, tmp_path):
        return _make_svc({"storage": {"jobs_dir": str(tmp_path / "jobs")}})

    def test_auto_loudnorm_forced_when_rms_below_threshold(self, tmp_path, monkeypatch):
        from pathlib import Path

        from transcria.audio.normalization import AudioNormalizationService

        svc = self._svc(tmp_path)
        monkeypatch.setattr(AudioNormalizationService, "should_normalize",
                            lambda self, mode: (False, ["mode_sans_normalisation"], []))
        monkeypatch.setattr(AudioNormalizationService, "weak_voice_filters",
                            lambda self, preflight: (False, [], []))
        applied = {}

        def fake_apply(self, in_path, out_path, filters):
            applied["filters"] = filters
            return out_path

        monkeypatch.setattr(AudioNormalizationService, "apply", fake_apply)

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")
        result = svc._run_audio_normalization(
            _job(), str(audio), "quality", MagicMock(), audio_preflight={"rms": 0.001}
        )

        assert Path(result).name == "normalized.wav"
        assert applied["filters"] == ["loudnorm=I=-23:TP=-2:LRA=11"]
        import json
        meta = json.loads((tmp_path / "jobs" / "test-job-001" / "metadata" /
                           "audio_normalization.json").read_text())
        assert meta["forced"] is True
        assert any(r.startswith("rms=") for r in meta["reasons"])

    def test_standard_path_kept_when_apply_returns_input(self, tmp_path, monkeypatch):
        from transcria.audio.normalization import AudioNormalizationService

        svc = self._svc(tmp_path)
        monkeypatch.setattr(AudioNormalizationService, "should_normalize",
                            lambda self, mode: (True, ["profil"], ["loudnorm"]))
        monkeypatch.setattr(AudioNormalizationService, "apply",
                            lambda self, in_path, out_path, filters: in_path)

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")
        result = svc._run_audio_normalization(_job(), str(audio), "quality", MagicMock())

        assert result == str(audio)

    def test_save_metadata_swallows_errors(self, monkeypatch):
        from transcria.services.pipeline_steps import normalization

        monkeypatch.setattr(normalization, "job_fs",
                            lambda config, job_id: (_ for _ in ()).throw(OSError("disque plein")))
        # Best-effort : jamais d'exception, même si le filesystem est indisponible.
        normalization.save_metadata({}, _job(), "in.wav", "out.wav", "quality", [], [])


class TestPreflightHelpers:
    """Lectures du signal préflight : artefact, RMS, replis silencieux."""

    def test_empty_analysis_returns_empty_dict(self, tmp_path, monkeypatch):
        from transcria.audio.preflight import AudioPreflightAnalyzer

        svc = _make_svc({"storage": {"jobs_dir": str(tmp_path / "jobs")}})
        monkeypatch.setattr(AudioPreflightAnalyzer, "analyze", lambda self, p: {})

        assert svc._run_audio_preflight(_job(), str(tmp_path / "a.wav"), MagicMock()) == {}

    def test_save_failure_still_returns_preflight(self, tmp_path, monkeypatch):
        from transcria.audio.preflight import AudioPreflightAnalyzer
        from transcria.services.pipeline_steps import preflight as preflight_step

        svc = _make_svc({"storage": {"jobs_dir": str(tmp_path / "jobs")}})
        payload = {"rms": 0.5, "risk_level": "ok"}
        monkeypatch.setattr(AudioPreflightAnalyzer, "analyze", lambda self, p: payload)
        monkeypatch.setattr(preflight_step, "job_fs",
                            lambda config, job_id: (_ for _ in ()).throw(OSError("disque plein")))

        assert svc._run_audio_preflight(_job(), str(tmp_path / "a.wav"), MagicMock()) == payload

    def test_load_audio_preflight_swallows_errors(self, monkeypatch):
        from transcria.services.pipeline_steps import preflight as preflight_step

        monkeypatch.setattr(preflight_step, "job_fs",
                            lambda config, job_id: (_ for _ in ()).throw(OSError("boom")))
        assert preflight_step.load_audio_preflight({}, _job()) == {}

    def test_rms_from_preflight_edge_cases(self):
        from transcria.services.pipeline_steps.preflight import rms_from_preflight

        assert rms_from_preflight(None) is None
        assert rms_from_preflight({}) is None
        assert rms_from_preflight({"rms": "pas-un-nombre"}) is None
        assert rms_from_preflight({"rms": None}) is None
        assert rms_from_preflight({"rms": "0.25"}) == 0.25

    def test_compute_rms_on_real_wav(self, tmp_path):
        import numpy as np
        import soundfile as sf

        from transcria.services.pipeline_steps.preflight import compute_rms

        path = tmp_path / "tone.wav"
        samples = 0.5 * np.sin(2 * np.pi * 440 * np.linspace(0, 0.1, 1600))
        sf.write(path, samples.astype("float32"), 16000)

        rms = compute_rms(str(path))
        assert rms == pytest.approx(0.5 / np.sqrt(2), rel=0.05)

    def test_compute_rms_returns_none_on_error(self):
        from transcria.services.pipeline_steps.preflight import compute_rms

        assert compute_rms("/chemin/inexistant.wav") is None


# ---------------------------------------------------------------------------
# _config_for_mode — exclusion Granite sur audio dégradé (B2 lot 2)
# ---------------------------------------------------------------------------


class TestConfigForModeGraniteDegradedExclusion:
    """Granite (expérimental) est exclu quand l'audio est dégradé : repli production."""

    def _svc(self, tmp_path, backend="granite"):
        return _make_svc({
            "models": {"stt_backend": backend},
            "storage": {"jobs_dir": str(tmp_path / "jobs")},
        })

    def _write_quality(self, tmp_path, job_id, quality=None, preflight=None):
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(str(tmp_path / "jobs"), job_id)
        if quality is not None:
            fs.save_json("metadata/audio_quality_decision.json", quality)
        if preflight is not None:
            fs.save_json("metadata/audio_preflight.json", preflight)

    def test_niveau_degrade_bascule_sur_cohere(self, tmp_path):
        svc = self._svc(tmp_path)
        job = _job()
        self._write_quality(tmp_path, job.id, quality={"level": "degrade"})

        effective = svc._config_for_mode("fast", job)

        # Source granite → granite interdit comme repli : cohere (production).
        assert effective["models"]["stt_backend"] == "cohere"

    def test_flag_audio_tres_faible_bascule_sur_cohere(self, tmp_path):
        svc = self._svc(tmp_path)
        job = _job()
        self._write_quality(tmp_path, job.id, preflight={"flags": ["audio_tres_faible"]})

        effective = svc._config_for_mode("fast", job)

        assert effective["models"]["stt_backend"] == "cohere"

    def test_audio_sain_conserve_granite(self, tmp_path):
        svc = self._svc(tmp_path)
        job = _job()
        self._write_quality(tmp_path, job.id, quality={"level": "ok"}, preflight={"flags": []})

        effective = svc._config_for_mode("fast", job)

        assert effective["models"]["stt_backend"] == "granite"

    def test_sans_job_conserve_granite(self, tmp_path):
        svc = self._svc(tmp_path)

        effective = svc._config_for_mode("fast", None)

        assert effective["models"]["stt_backend"] == "granite"

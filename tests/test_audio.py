"""Tests pour les modules audio : séparation de sources, VAD adaptatif."""

import os

import pytest


# ---------------------------------------------------------------------------
# AudioPreflightAnalyzer
# ---------------------------------------------------------------------------


class TestAudioPreflightAnalyzer:
    """Pré-diagnostic audio déterministe avant STT."""

    def _write_wav(self, path, signal, sample_rate=16000):
        sf = pytest.importorskip("soundfile")
        sf.write(path, signal, sample_rate)

    def test_disabled_preflight_returns_empty_dict(self, tmp_path):
        from transcria.audio.preflight import AudioPreflightAnalyzer

        audio = tmp_path / "audio.wav"
        self._write_wav(audio, [0.0] * 1600)

        result = AudioPreflightAnalyzer({"workflow": {"audio_preflight": {"enabled": False}}}).analyze(audio)

        assert result == {}

    def test_very_low_rms_sets_unreliable_transcription_risk(self, tmp_path):
        np = pytest.importorskip("numpy")
        from transcria.audio.preflight import AudioPreflightAnalyzer

        sr = 16000
        t = np.arange(sr, dtype="float32") / sr
        audio = tmp_path / "quiet.wav"
        self._write_wav(audio, 0.002 * np.sin(2 * np.pi * 1000 * t), sr)

        result = AudioPreflightAnalyzer({}).analyze(audio)

        assert result["rms"] < 0.008
        assert "audio_tres_faible" in result["flags"]
        assert "risque_transcription_non_fiable" in result["flags"]
        assert result["risk_level"] == "degrade"

    def test_wide_clean_signal_has_bandwidth_metrics_without_low_volume_flag(self, tmp_path):
        np = pytest.importorskip("numpy")
        from transcria.audio.preflight import AudioPreflightAnalyzer

        sr = 16000
        t = np.arange(sr, dtype="float32") / sr
        signal = (
            0.12 * np.sin(2 * np.pi * 1000 * t)
            + 0.08 * np.sin(2 * np.pi * 6000 * t)
        )
        audio = tmp_path / "wide.wav"
        self._write_wav(audio, signal, sr)

        result = AudioPreflightAnalyzer({}).analyze(audio)

        assert result["rms"] > 0.02
        assert result["bandwidth_99_hz"] > 3800
        assert "audio_faible" not in result["flags"]
        assert "audio_tres_faible" not in result["flags"]
        assert result["risk_level"] == "ok"

    def test_clipping_is_flagged(self, tmp_path):
        np = pytest.importorskip("numpy")
        from transcria.audio.preflight import AudioPreflightAnalyzer

        signal = np.ones(16000, dtype="float32")
        audio = tmp_path / "clipped.wav"
        self._write_wav(audio, signal)

        result = AudioPreflightAnalyzer({}).analyze(audio)

        assert result["clipping_ratio"] > 0.001
        assert "clipping_detecte" in result["flags"]
        assert result["risk_level"] == "degrade"

    def test_bandwidth_uses_active_frames_not_leading_silence(self, tmp_path):
        np = pytest.importorskip("numpy")
        from transcria.audio.preflight import AudioPreflightAnalyzer

        sr = 16000
        t = np.arange(sr, dtype="float32") / sr
        silence = np.zeros(sr, dtype="float32")
        active = 0.12 * np.sin(2 * np.pi * 5000 * t)
        audio = tmp_path / "silence_then_wide.wav"
        self._write_wav(audio, np.concatenate([silence, active]), sr)

        result = AudioPreflightAnalyzer({}).analyze(audio)

        assert result["bandwidth_99_hz"] > 3800


# ---------------------------------------------------------------------------
# SourceSeparationDecider
# ---------------------------------------------------------------------------


class TestSourceSeparationDecider:
    """Décision score-based : faut-il lancer Demucs avant la transcription ?"""

    def _cfg(self, min_score=2, min_duration_s=60):
        return {
            "workflow": {
                "source_separation": {
                    "decision": {
                        "min_score": min_score,
                        "min_duration_s": min_duration_s,
                        "scene_music_min_ratio": 0.05,
                        "scene_music_min_duration_s": 10,
                        "scene_noise_score_ratio": 0.35,
                        "scene_noise_score": 1,
                        "scene_problem_segments_score_threshold": 3,
                        "scene_problem_segments_score": 1,
                    }
                }
            }
        }

    def test_vad_peu_selectif_alone_triggers_separation(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, reasons = decider.should_separate(
            {"duration_seconds": 180},
            {"level": "suspect", "reasons": ["vad_peu_selectif"]},
        )
        assert should is True
        assert "vad_peu_selectif" in reasons

    def test_non_latin_segments_contributes_to_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 180},
            {"level": "degrade", "reasons": ["segments_non_latins"]},
        )
        assert should is True
        assert any("non_latin" in r or "degrade" in r for r in reasons)

    def test_clean_audio_returns_false(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 600},
            {"level": "ok", "reasons": []},
        )
        assert should is False

    def test_short_audio_is_blocked_regardless_of_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        # min_score=1 : moindre signal suffit normalement, mais audio trop court
        decider = SourceSeparationDecider(self._cfg(min_score=1, min_duration_s=120))
        should, reasons = decider.should_separate(
            {"duration_seconds": 30},
            {"level": "degrade", "reasons": ["vad_peu_selectif", "segments_non_latins"]},
        )
        assert should is False
        assert any("court" in r for r in reasons)

    def test_always_returns_list_of_reasons(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg())
        _, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
        )
        assert isinstance(reasons, list)

    def test_vad_agressif_does_not_trigger_separation(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        # vad_agressif = audio trop silencieux / trop sparse — la séparation n'aide pas
        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "suspect", "reasons": ["vad_agressif"]},
        )
        assert should is False

    def test_scene_music_detected_overrides_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        # Musique au-dessus du seuil → séparer, même si le score seul ne suffit pas
        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
            audio_scene={"has_music": True, "speech_ratio": 0.3, "music_ratio": 0.08},
        )
        assert should is True
        assert any("musique" in r for r in reasons)

    def test_scene_music_with_too_little_speech_is_not_forced(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, reasons = decider.should_separate(
            {"duration_seconds": 600},
            {"level": "ok", "reasons": []},
            audio_scene={
                "has_music": True,
                "speech_ratio": 0.015,
                "music_ratio": 0.98,
                "stats": {"total_duration_s": 600},
            },
        )
        assert should is False
        assert any("parole_faible" in r for r in reasons)

    def test_low_speech_music_false_positive_does_not_score_problem_segments(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, reasons = decider.should_separate(
            {"duration_seconds": 600},
            {"level": "degrade", "reasons": ["segments_courts_nombreux"]},
            audio_scene={
                "has_music": True,
                "speech_ratio": 0.015,
                "music_ratio": 0.98,
                "problem_segments": [{"label": "music"} for _ in range(124)],
                "stats": {"total_duration_s": 600},
            },
        )
        assert should is False
        assert any("parole_faible" in r for r in reasons)
        assert not any("scene_zones_problematiques" in r for r in reasons)

    def test_scene_music_with_enough_speech_can_still_force(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, reasons = decider.should_separate(
            {"duration_seconds": 600},
            {"level": "ok", "reasons": []},
            audio_scene={
                "has_music": True,
                "speech_ratio": 0.20,
                "music_ratio": 0.98,
                "stats": {"total_duration_s": 600},
            },
        )
        assert should is True
        assert any(r.startswith("scene_musique:") for r in reasons)

    def test_scene_music_below_threshold_does_not_override_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
            audio_scene={"has_music": True, "speech_ratio": 0.9, "music_ratio": 0.01},
        )
        assert should is False
        assert reasons == []

    def test_scene_music_duration_can_override_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, reasons = decider.should_separate(
            {"duration_seconds": 600},
            {"level": "ok", "reasons": []},
            audio_scene={
                "has_music": True,
                "music_ratio": 0.02,
                "stats": {"total_duration_s": 600},
            },
        )
        assert should is True
        assert any("duration_s=12.0" in r for r in reasons)

    def test_scene_noise_contributes_to_score_without_forcing_alone(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
            audio_scene={"has_noise": True, "noise_ratio": 0.40},
        )
        assert should is False
        assert any("scene_bruit" in r for r in reasons)

    def test_scene_noise_and_quality_signal_can_reach_score(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "suspect", "reasons": ["segments_courts_nombreux"]},
            audio_scene={"has_noise": True, "noise_ratio": 0.40},
        )
        assert should is True
        assert any("scene_bruit" in r for r in reasons)
        assert "segments_courts_nombreux" in reasons

    def test_scene_no_music_does_not_force_separation(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        # Pas de musique → le signal scene n'impose pas la séparation (score décide)
        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
            audio_scene={"has_music": False, "speech_ratio": 0.95},
        )
        assert should is False

    def test_falls_back_to_score_when_scene_is_none(self):
        from transcria.audio.source_separation import SourceSeparationDecider

        # Sans scene : logique score existante inchangée
        decider = SourceSeparationDecider(self._cfg(min_score=3))
        should, _ = decider.should_separate(
            {"duration_seconds": 180},
            {"level": "suspect", "reasons": ["vad_peu_selectif"]},
            audio_scene=None,
        )
        assert should is True


# ---------------------------------------------------------------------------
# SourceSeparationService
# ---------------------------------------------------------------------------


class TestSourceSeparationService:
    """Comportement du service : dégradation gracieuse, chemins de sortie."""

    def test_available_property_is_bool(self):
        from transcria.audio.source_separation import SourceSeparationService

        svc = SourceSeparationService({})
        assert isinstance(svc.available, bool)

    def test_disabled_returns_original_path(self, tmp_path):
        from transcria.audio.source_separation import SourceSeparationService

        audio = tmp_path / "audio.wav"
        audio.touch()
        cfg = {"workflow": {"source_separation": {"enabled": False, "backend": "demucs"}}}
        result = SourceSeparationService(cfg).separate(audio, tmp_path / "vocals.wav")
        assert result == audio

    def test_unavailable_returns_original_path(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationService

        audio = tmp_path / "audio.wav"
        audio.touch()
        cfg = {"workflow": {"source_separation": {"enabled": True, "backend": "demucs"}}}
        svc = SourceSeparationService(cfg)
        monkeypatch.setattr(type(svc), "available", property(lambda self: False))
        result = svc.separate(audio, tmp_path / "vocals.wav")
        assert result == audio

    def test_separation_exception_returns_original_path(self, tmp_path, monkeypatch):
        from transcria.audio.source_separation import SourceSeparationService

        audio = tmp_path / "audio.wav"
        audio.touch()
        cfg = {"workflow": {"source_separation": {"enabled": True, "backend": "demucs"}}}
        svc = SourceSeparationService(cfg)
        monkeypatch.setattr(type(svc), "available", property(lambda self: True))
        monkeypatch.setattr(svc, "_run_separation", lambda *_: (_ for _ in ()).throw(RuntimeError("échec demucs")))
        result = svc.separate(audio, tmp_path / "vocals.wav")
        assert result == audio

    def test_separate_with_demucs_installed(self, tmp_path, monkeypatch):
        """Vérifie la chaîne complète quand demucs est disponible (mock modèle)."""
        if os.getenv("TRANSCRIA_REQUIRE_DEMUCS_TEST") == "1":
            import demucs  # noqa: F401
        else:
            pytest.importorskip("demucs")
        torch = pytest.importorskip("torch")
        try:
            import torchaudio
        except (ImportError, OSError) as exc:
            pytest.skip(f"torchaudio indisponible ou incompatible: {exc}")

        from transcria.audio.source_separation import SourceSeparationService

        # Créer un faux fichier WAV utilisable par torchaudio
        audio = tmp_path / "audio.wav"
        torchaudio.save(str(audio), torch.zeros(1, 16000), 16000)

        cfg = {"workflow": {"source_separation": {
            "enabled": True,
            "backend": "demucs",
            "model": "htdemucs",
            "device": "cpu",
            "segment_s": 5,
            "stem": "vocals",
        }}}
        output = tmp_path / "vocals.wav"

        # Mock get_model pour ne pas télécharger le vrai modèle
        class FakeModel:
            samplerate = 16000
            audio_channels = 1
            sources = ["drums", "bass", "other", "vocals"]

            def eval(self):
                return self

            def to(self, device):
                return self

        fake_sources = torch.zeros(1, 4, 1, 16000)  # (batch, stems, channels, samples)

        monkeypatch.setattr("demucs.pretrained.get_model", lambda name: FakeModel())
        monkeypatch.setattr("demucs.apply.apply_model", lambda model, mix, **kw: fake_sources)
        monkeypatch.setattr(
            "demucs.audio.convert_audio",
            lambda wav, from_sr, to_sr, to_ch: wav,
        )

        result = SourceSeparationService(cfg).separate(audio, output)
        assert result == output
        assert output.exists()


# ---------------------------------------------------------------------------
# AudioSceneFilterService
# ---------------------------------------------------------------------------


class TestAudioSceneFilterService:
    """Filtrage pré-STT par silence, sans changement de durée."""

    def _cfg(self, enabled=True):
        return {
            "workflow": {
                "audio_scene_filter": {
                    "enabled": enabled,
                    "enabled_for_modes": ["quality"],
                    "target_labels": ["music", "noise"],
                    "min_segment_s": 2.0,
                    "min_total_muted_s": 2.0,
                    "edge_keep_s": 0.15,
                    "max_intervals": 10,
                    "timeout_s": 30,
                }
            }
        }

    def test_disabled_filter_returns_false(self):
        from transcria.audio.scene_filter import AudioSceneFilterService

        service = AudioSceneFilterService(self._cfg(enabled=False))
        should, reasons, intervals = service.should_filter("quality", {"problem_segments": []})

        assert should is False
        assert reasons == ["filtre_desactive"]
        assert intervals == []

    def test_filter_only_runs_for_enabled_modes(self):
        from transcria.audio.scene_filter import AudioSceneFilterService

        service = AudioSceneFilterService(self._cfg())
        should, reasons, _ = service.should_filter("fast", {"problem_segments": []})

        assert should is False
        assert reasons == ["mode_non_active:fast"]

    def test_builds_intervals_from_target_problem_segments(self):
        from transcria.audio.scene_filter import AudioSceneFilterService

        service = AudioSceneFilterService(self._cfg())
        should, reasons, intervals = service.should_filter("quality", {
            "problem_segments": [
                {"label": "noise", "start": 10.0, "end": 14.0},
                {"label": "noEnergy", "start": 20.0, "end": 30.0},
                {"label": "music", "start": 40.0, "end": 41.0},
            ]
        })

        assert should is True
        assert reasons == ["intervals=1", "muted_s=3.7"]
        assert intervals == [
            {"label": "noise", "start": 10.15, "end": 13.85, "duration_s": 3.7}
        ]

    def test_apply_returns_original_on_ffmpeg_failure(self, tmp_path, monkeypatch):
        import subprocess
        from transcria.audio.scene_filter import AudioSceneFilterService

        input_path = tmp_path / "input.wav"
        output_path = tmp_path / "filtered.wav"
        input_path.write_bytes(b"audio")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, [])),
        )

        service = AudioSceneFilterService(self._cfg())
        result = service.apply(input_path, output_path, [
            {"label": "noise", "start": 1.0, "end": 3.0, "duration_s": 2.0}
        ])

        assert result == input_path


# ---------------------------------------------------------------------------
# AudioNormalizationService
# ---------------------------------------------------------------------------


class TestAudioNormalizationService:
    """Normalisation ffmpeg légère, paramétrable et désactivée par défaut."""

    def _cfg(self, enabled=True):
        return {
            "workflow": {
                "audio_normalization": {
                    "enabled": enabled,
                    "enabled_for_modes": ["quality"],
                    "loudnorm_enabled": True,
                    "target_i": -23.0,
                    "true_peak": -2.0,
                    "lra": 11.0,
                    "highpass_hz": 80,
                    "timeout_s": 30,
                }
            }
        }

    def test_disabled_normalization_returns_false(self):
        from transcria.audio.normalization import AudioNormalizationService

        service = AudioNormalizationService(self._cfg(enabled=False))
        should, reasons, filters = service.should_normalize("quality")

        assert should is False
        assert reasons == ["normalisation_desactivee"]
        assert filters == []

    def test_normalization_only_runs_for_enabled_modes(self):
        from transcria.audio.normalization import AudioNormalizationService

        service = AudioNormalizationService(self._cfg())
        should, reasons, _ = service.should_normalize("fast")

        assert should is False
        assert reasons == ["mode_non_active:fast"]

    def test_builds_configured_filters(self):
        from transcria.audio.normalization import AudioNormalizationService

        service = AudioNormalizationService(self._cfg())
        should, reasons, filters = service.should_normalize("quality")

        assert should is True
        assert reasons == ["filters=2"]
        assert filters == ["highpass=f=80", "loudnorm=I=-23:TP=-2:LRA=11"]

    def test_weak_voice_filters_use_preflight_rms_and_bounded_gain(self):
        from transcria.audio.normalization import AudioNormalizationService

        service = AudioNormalizationService({
            "workflow": {
                "audio_normalization": {
                    "weak_voice": {
                        "enabled": True,
                        "target_rms": 0.05,
                        "max_gain": 8.0,
                        "loudnorm_after_gain": True,
                    }
                }
            }
        })
        should, reasons, filters = service.weak_voice_filters({
            "rms": 0.005,
            "flags": ["audio_tres_faible"],
        })

        assert should is True
        assert "gain=8.000" in reasons
        assert filters[0] == "volume=8.000"
        assert filters[1].startswith("loudnorm=")

    def test_no_configured_filters_returns_false(self):
        from transcria.audio.normalization import AudioNormalizationService

        cfg = {
            "workflow": {
                "audio_normalization": {
                    "enabled": True,
                    "enabled_for_modes": ["quality"],
                    "loudnorm_enabled": False,
                    "highpass_hz": None,
                }
            }
        }
        service = AudioNormalizationService(cfg)
        should, reasons, filters = service.should_normalize("quality")

        assert should is False
        assert reasons == ["aucun_filtre_configure"]
        assert filters == []

    def test_apply_returns_original_on_ffmpeg_failure(self, tmp_path, monkeypatch):
        import subprocess
        from transcria.audio.normalization import AudioNormalizationService

        input_path = tmp_path / "input.wav"
        output_path = tmp_path / "normalized.wav"
        input_path.write_bytes(b"audio")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, [])),
        )

        service = AudioNormalizationService(self._cfg())
        result = service.apply(input_path, output_path, ["loudnorm=I=-23:TP=-2:LRA=11"])

        assert result == input_path


# ---------------------------------------------------------------------------
# AudioDenoiseService
# ---------------------------------------------------------------------------


class TestAudioDenoiseService:
    """Débruitage expérimental désactivé par défaut."""

    def _cfg(self, enabled=True):
        return {
            "workflow": {
                "audio_denoise": {
                    "enabled": enabled,
                    "enabled_for_modes": ["quality"],
                    "backend": "ffmpeg_afftdn",
                    "force": False,
                    "trigger_flags": ["snr_faible"],
                    "noise_reduction_db": 12.0,
                    "noise_floor_db": -25.0,
                    "timeout_s": 30,
                }
            }
        }

    def test_disabled_denoise_returns_false(self):
        from transcria.audio.denoise import AudioDenoiseService

        should, reasons, filters = AudioDenoiseService(self._cfg(enabled=False)).should_denoise(
            "quality", {"flags": ["snr_faible"]}
        )

        assert should is False
        assert reasons == ["denoise_desactive"]
        assert filters == []

    def test_denoise_requires_trigger_flag(self):
        from transcria.audio.denoise import AudioDenoiseService

        should, reasons, filters = AudioDenoiseService(self._cfg()).should_denoise(
            "quality", {"flags": ["audio_faible"]}
        )

        assert should is False
        assert reasons == ["preflight_sans_bruit_declencheur"]
        assert filters == []

    def test_denoise_builds_afftdn_filter_for_low_snr(self):
        from transcria.audio.denoise import AudioDenoiseService

        should, reasons, filters = AudioDenoiseService(self._cfg()).should_denoise(
            "quality", {"flags": ["snr_faible"]}
        )

        assert should is True
        assert reasons == ["preflight:snr_faible"]
        assert filters == ["afftdn=nr=12:nf=-25"]


# ---------------------------------------------------------------------------
# HysteresisBinarizer
# ---------------------------------------------------------------------------


class TestHysteresisBinarizer:
    """Seuillage VAD onset/offset avec fusion des gaps courts."""

    def test_binarize_uses_distinct_onset_offset(self):
        from transcria.audio.vad_hysteresis import HysteresisBinarizer

        binarizer = HysteresisBinarizer(
            onset=0.6,
            offset=0.4,
            frame_s=0.1,
            min_duration_on=0.1,
            min_duration_off=0.15,
        )

        segments = binarizer.binarize([0.1, 0.7, 0.5, 0.45, 0.3])

        assert segments == [{"start": 0.1, "end": 0.4}]


# ---------------------------------------------------------------------------
# AudioSceneWorker — fonctions pures (importables sans dépendance audio lourde)
# ---------------------------------------------------------------------------


class TestAudioSceneWorkerStats:
    """_compute_stats, _compute_gender_stats et _compute_signals sont des fonctions
    pures testables sans charger les dépendances d'analyse audio."""

    def _mixed_segments(self):
        return [
            ("speech", 0.0, 5.0),
            ("music", 5.0, 8.0),
            ("speech", 8.0, 12.0),
            ("noise", 12.0, 13.0),
        ]

    def test_compute_stats_sums_durations_per_label(self):
        from transcria.audio._scene_analysis_worker import _compute_stats

        stats = _compute_stats(self._mixed_segments())
        assert stats["labels"]["speech"]["duration_s"] == pytest.approx(9.0)
        assert stats["labels"]["music"]["duration_s"] == pytest.approx(3.0)
        assert stats["labels"]["noise"]["duration_s"] == pytest.approx(1.0)

    def test_compute_stats_ratio_sums_to_one(self):
        from transcria.audio._scene_analysis_worker import _compute_stats

        stats = _compute_stats(self._mixed_segments())
        total = sum(v["ratio"] for v in stats["labels"].values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_compute_gender_stats_ratio_of_speech(self):
        from transcria.audio._scene_analysis_worker import _compute_gender_stats

        segs = [("male", 0.0, 6.0), ("female", 6.0, 10.0)]
        g = _compute_gender_stats(segs)

        assert g["has_gender_data"] is True
        assert g["male_ratio"] == pytest.approx(0.6)
        assert g["female_ratio"] == pytest.approx(0.4)
        assert g["dominant"] == "male"

    def test_compute_gender_stats_empty_when_no_gender_label(self):
        from transcria.audio._scene_analysis_worker import _compute_gender_stats

        segs = [("speech", 0.0, 10.0), ("music", 10.0, 15.0)]
        g = _compute_gender_stats(segs)

        assert g["has_gender_data"] is False
        assert g["male_ratio"] == pytest.approx(0.0)
        assert g["female_ratio"] == pytest.approx(0.0)
        assert g["dominant"] is None

    def test_compute_signals_has_music_flag(self):
        from transcria.audio._scene_analysis_worker import _compute_signals

        stats = {
            "labels": {
                "speech": {"duration_s": 8.0, "ratio": 0.62},
                "music": {"duration_s": 5.0, "ratio": 0.38},
            },
            "total_duration_s": 13.0,
        }
        gender_stats = {"has_gender_data": False, "male_ratio": 0.0, "female_ratio": 0.0, "dominant": None}
        signals = _compute_signals(stats, gender_stats)

        assert signals["has_music"] is True
        assert signals["has_noise"] is False

    def test_compute_signals_speech_ratio_includes_male_and_female(self):
        from transcria.audio._scene_analysis_worker import _compute_signals

        # Quand detect_gender=True, l'analyse pitch remplace "speech" par "male"/"female"
        stats = {
            "labels": {
                "male": {"duration_s": 6.0, "ratio": 0.6},
                "female": {"duration_s": 4.0, "ratio": 0.4},
            },
            "total_duration_s": 10.0,
        }
        gender_stats = {"has_gender_data": True, "male_ratio": 0.6, "female_ratio": 0.4, "dominant": "male"}
        signals = _compute_signals(stats, gender_stats)

        assert signals["speech_ratio"] == pytest.approx(1.0)
        assert signals["has_music"] is False

    def test_compute_signals_exposes_non_speech_ratios(self):
        from transcria.audio._scene_analysis_worker import _compute_signals

        stats = {
            "labels": {
                "speech": {"duration_s": 10.0, "ratio": 0.5},
                "music": {"duration_s": 4.0, "ratio": 0.2},
                "noise": {"duration_s": 2.0, "ratio": 0.1},
                "noEnergy": {"duration_s": 4.0, "ratio": 0.2},
            },
            "total_duration_s": 20.0,
        }
        gender_stats = {"has_gender_data": False, "male_ratio": 0.0, "female_ratio": 0.0, "dominant": None}

        signals = _compute_signals(stats, gender_stats)

        assert signals["speech_ratio"] == pytest.approx(0.5)
        assert signals["music_ratio"] == pytest.approx(0.2)
        assert signals["noise_ratio"] == pytest.approx(0.1)
        assert signals["no_energy_ratio"] == pytest.approx(0.2)
        assert signals["non_speech_ratio"] == pytest.approx(0.5)

    def test_segments_to_dicts_adds_stable_duration(self):
        from transcria.audio._scene_analysis_worker import _segments_to_dicts

        out = _segments_to_dicts([("noise", 1.12345, 3.98765)])

        assert out == [
            {"label": "noise", "start": 1.123, "end": 3.988, "duration_s": 2.864}
        ]

    def test_problem_segments_keeps_only_long_non_speech_segments(self):
        from transcria.audio._scene_analysis_worker import _problem_segments

        segments = [
            ("speech", 0.0, 10.0),
            ("music", 10.0, 11.0),
            ("noise", 11.0, 14.5),
            ("noEnergy", 14.5, 17.0),
        ]

        out = _problem_segments(segments, min_duration_s=2.0)

        assert out == [
            {"label": "noise", "start": 11.0, "end": 14.5, "duration_s": 3.5},
            {"label": "noEnergy", "start": 14.5, "end": 17.0, "duration_s": 2.5},
        ]


# ---------------------------------------------------------------------------
# AudioSceneAnalyzer — service subprocess
# ---------------------------------------------------------------------------


class TestAudioSceneAnalyzer:
    """Comportement du service : dégradation gracieuse, parsing JSON du worker."""

    def _cfg(self, enabled=True, timeout_s=30):
        return {
            "workflow": {
                "audio_scene": {
                    "enabled": enabled,
                    "timeout_s": timeout_s,
                }
            }
        }

    def test_disabled_returns_empty_dict(self, tmp_path):
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        audio = tmp_path / "audio.wav"
        audio.touch()
        result = AudioSceneAnalyzer(self._cfg(enabled=False)).analyze(audio)
        assert result == {}

    def test_available_is_bool(self):
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        assert isinstance(AudioSceneAnalyzer(self._cfg()).available, bool)

    def test_analyze_worker_failure_returns_empty_dict(self, tmp_path, monkeypatch):
        import subprocess
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        audio = tmp_path / "audio.wav"
        audio.touch()
        analyzer = AudioSceneAnalyzer(self._cfg())

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"crash"),
        )
        assert analyzer.analyze(audio) == {}

    def test_analyze_timeout_returns_empty_dict(self, tmp_path, monkeypatch):
        import subprocess
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        audio = tmp_path / "audio.wav"
        audio.touch()
        analyzer = AudioSceneAnalyzer(self._cfg(timeout_s=1))

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired([], 1)),
        )
        assert analyzer.analyze(audio) == {}

    def test_analyze_parses_valid_json_from_worker_stdout(self, tmp_path, monkeypatch):
        import json
        import subprocess
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        audio = tmp_path / "audio.wav"
        audio.touch()
        analyzer = AudioSceneAnalyzer(self._cfg())

        expected = {
            "has_music": False,
            "has_noise": False,
            "speech_ratio": 0.92,
            "gender": {
                "has_gender_data": True,
                "dominant": "male",
                "male_ratio": 0.7,
                "female_ratio": 0.3,
            },
        }
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(expected).encode(), stderr=b""
        )
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake)
        assert analyzer.analyze(audio) == expected

    def test_analyze_sets_stable_worker_environment(self, tmp_path, monkeypatch):
        import json
        import subprocess
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        audio = tmp_path / "audio.wav"
        audio.touch()
        analyzer = AudioSceneAnalyzer(self._cfg())
        captured_env = {}

        def fake_run(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"gender_segments": []}).encode(),
                stderr=b"",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        assert analyzer.analyze(audio) == {"gender_segments": []}
        assert captured_env.get("NUMBA_CACHE_DIR")


# ---------------------------------------------------------------------------
# _scene_analysis_worker — champ gender_segments dans l'output JSON
# ---------------------------------------------------------------------------


class TestSceneWorkerGenderSegments:
    """Vérifie que le bloc __main__ du worker expose gender_segments horodatés."""

    def _run_worker(self, segments_fixture, detect_gender=True, monkeypatch=None):
        """Appelle _analyze_audio via mock et reconstitue le JSON comme le bloc __main__."""
        import json
        import subprocess
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        payload = {
            "has_music": False,
            "has_noise": False,
            "speech_ratio": 0.8,
            "music_ratio": 0.0,
            "noise_ratio": 0.05,
            "no_energy_ratio": 0.0,
            "non_speech_ratio": 0.05,
            "gender": {
                "has_gender_data": detect_gender and bool(segments_fixture),
                "dominant": "female" if segments_fixture else None,
                "male_ratio": 0.3,
                "female_ratio": 0.7,
            },
            "stats": {
                "labels": {
                    "male": {"duration_s": 3.0, "ratio": 0.3},
                    "female": {"duration_s": 7.0, "ratio": 0.7},
                    "noise": {"duration_s": 0.5, "ratio": 0.05},
                },
                "total_duration_s": 10.5,
            },
            "scene_segments": [
                {"label": "female", "start": 0.0, "end": 2.5, "duration_s": 2.5},
                {"label": "noise", "start": 2.5, "end": 3.0, "duration_s": 0.5},
                {"label": "male", "start": 3.0, "end": 5.0, "duration_s": 2.0},
            ],
            "problem_segments": [],
            "gender_segments": segments_fixture,
        }
        return payload

    def test_gender_segments_present_in_output(self):
        segs = [
            {"start": 0.0, "end": 2.5, "label": "female"},
            {"start": 3.0, "end": 5.0, "label": "male"},
        ]
        out = self._run_worker(segs)
        assert "gender_segments" in out

    def test_gender_segments_only_contains_male_female(self):
        segs = [
            {"start": 0.0, "end": 2.5, "label": "female"},
            {"start": 3.0, "end": 5.0, "label": "male"},
        ]
        out = self._run_worker(segs)
        labels = {s["label"] for s in out["gender_segments"]}
        assert labels <= {"male", "female"}

    def test_gender_segments_empty_when_no_gender(self):
        out = self._run_worker([], detect_gender=False)
        assert out["gender_segments"] == []

    def test_worker_main_block_includes_gender_segments(self, tmp_path, monkeypatch):
        """Intégration : appel du bloc __main__ via subprocess, vérifie gender_segments dans stdout."""
        import json
        import subprocess
        import sys
        import wave

        worker = (
            __import__("pathlib").Path(__file__).parent.parent
            / "transcria" / "audio" / "_scene_analysis_worker.py"
        )
        audio = tmp_path / "silence.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 16000)

        env = os.environ.copy()
        env.setdefault("NUMBA_CACHE_DIR", str(tmp_path / "numba_cache"))
        result = subprocess.run(
            [sys.executable, str(worker), str(audio)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "gender_segments" in data
        assert isinstance(data["gender_segments"], list)
        assert "scene_segments" in data
        assert isinstance(data["scene_segments"], list)
        assert "problem_segments" in data
        assert isinstance(data["problem_segments"], list)

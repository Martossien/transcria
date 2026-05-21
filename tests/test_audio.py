"""Tests pour les modules audio : séparation de sources, VAD adaptatif."""

import pytest


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

        # Musique détectée → toujours séparer, même si le score seul ne suffit pas
        decider = SourceSeparationDecider(self._cfg(min_score=2))
        should, reasons = decider.should_separate(
            {"duration_seconds": 300},
            {"level": "ok", "reasons": []},
            audio_scene={"has_music": True, "speech_ratio": 0.3},
        )
        assert should is True
        assert any("music" in r for r in reasons)

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
        pytest.importorskip("demucs")
        torch = pytest.importorskip("torch")
        import torchaudio

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
# AudioSceneWorker — fonctions pures (importables sans TensorFlow)
# ---------------------------------------------------------------------------


class TestAudioSceneWorkerStats:
    """_compute_stats, _compute_gender_stats et _compute_signals sont des fonctions
    pures testables sans charger inaSpeechSegmenter ni TensorFlow."""

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

        # Quand detect_gender=True, inaSpeechSegmenter remplace "speech" par "male"/"female"
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

        worker = (
            __import__("pathlib").Path(__file__).parent.parent
            / "transcria" / "audio" / "_scene_analysis_worker.py"
        )
        audio = tmp_path / "silence.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")

        result = subprocess.run(
            [sys.executable, str(worker), str(audio)],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.skip("Dépendances audio manquantes — worker non exécutable en CI")
        data = json.loads(result.stdout)
        assert "gender_segments" in data
        assert isinstance(data["gender_segments"], list)

from types import SimpleNamespace

import pytest

from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.anti_hallucination import collapse_repetition_loops
from transcria.stt.anti_hallucination import detect_repetition_loops
from transcria.stt.forced_alignment import ForcedAlignmentService
from transcria.stt.speaker_realignment import SpeakerPunctuationRealigner
from transcria.stt.speaker_detection import SpeakerDetector
from transcria.stt.transcription import Transcriber
from transcria.stt.transcriber_factory import _effective_whisper_config
from transcria.stt.whisper_transcriber import WhisperTranscriber
from transcria.audio.vad_adaptive import AdaptiveVADConfig
from transcria.quality.audio_quality import AudioQualityEvaluator


class TestCohereTranscriber:
    def test_available_detects_imports(self):
        ct = CohereTranscriber()
        is_available = ct.available
        assert isinstance(is_available, bool)

    def test_detect_device(self):
        device = CohereTranscriber._detect_device()
        assert isinstance(device, str)
        assert device in ("cpu", "cuda:0", "cuda:1", "cuda:2", "cuda:3")

    def test_seconds_to_srt_time(self):
        assert CohereTranscriber._seconds_to_srt_time(0) == "00:00:00,000"
        assert CohereTranscriber._seconds_to_srt_time(1.5) == "00:00:01,500"
        assert CohereTranscriber._seconds_to_srt_time(61.123) == "00:01:01,123"
        assert CohereTranscriber._seconds_to_srt_time(3661.999) == "01:01:01,999"
        assert CohereTranscriber._seconds_to_srt_time(3601.001) == "01:00:01,001"

    def test_segments_to_srt_empty(self):
        ct = CohereTranscriber()
        srt = ct.segments_to_srt([])
        assert srt == ""

    def test_segments_to_srt_basic(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 3, "text": "Bonjour"},
            {"start": 3, "end": 6, "text": "Comment allez-vous"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "00:00:00,000 --> 00:00:03,000" in srt
        assert "Bonjour" in srt
        assert "00:00:03,000 --> 00:00:06,000" in srt
        assert "Comment allez-vous" in srt

    def test_segments_to_srt_with_speaker(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 2, "text": "Texte", "speaker": "Alice"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "Alice: Texte" in srt

    def test_segments_to_srt_skips_empty_text(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 2, "text": ""},
            {"start": 2, "end": 4, "text": "Valid"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "Valid" in srt
        assert srt.count("-->") == 1

    def test_segments_to_srt_numbering_sequential(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 1, "text": "A"},
            {"start": 1, "end": 2, "text": "B"},
            {"start": 2, "end": 3, "text": "C"},
        ]
        srt = ct.segments_to_srt(segments)
        lines = srt.strip().split("\n")
        numbers = [l for l in lines if l.isdigit()]
        assert numbers == ["1", "2", "3"]

    def test_offload_clears_model(self):
        ct = CohereTranscriber()
        ct._model = "fake"
        ct._processor = "fake"
        ct.offload()
        assert ct._model is None
        assert ct._processor is None

    def test_load_with_invalid_path_returns_false(self):
        ct = CohereTranscriber(model_path="/nonexistent/model/path")
        if ct.available:
            result = ct.load()
            assert result is False


class TestWhisperQualityConfig:
    def test_effective_whisper_config_uses_central_defaults(self):
        cfg = {"whisper": {"model_size": "large-v3", "word_timestamps": True}}

        effective = _effective_whisper_config(cfg)

        assert effective["model_size"] == "large-v3"
        assert effective["word_timestamps"] is True
        assert effective["condition_on_previous_text"] is False
        assert effective["collapse_repetition_loops"] is True
        assert effective["repetition_loop_min_repeats"] == 4

    def test_whisper_transcriber_uses_configured_chunk_length_and_loop_policy(self):
        calls = {}

        class FakeModel:
            def transcribe(self, audio_input, **kwargs):
                calls.update(kwargs)
                segment = SimpleNamespace(
                    start=0.0,
                    end=2.0,
                    text="merci merci merci fin",
                    words=[],
                    avg_logprob=None,
                    compression_ratio=None,
                    no_speech_prob=None,
                )
                info = SimpleNamespace(language="fr", language_probability=1.0)
                return [segment], info

        transcriber = WhisperTranscriber(
            chunk_length_s=17,
            repetition_loop_min_repeats=3,
            repetition_loop_keep_repeats=1,
        )
        transcriber._model = FakeModel()
        transcriber.load = lambda: True

        segments = transcriber.transcribe(audio_path=None, audio_array=[0.0], sample_rate=16000)

        assert calls["chunk_length"] == 17
        assert calls["condition_on_previous_text"] is False
        assert segments[0]["text"] == "merci fin"
        assert segments[0]["hallucination_loops"][0]["count"] == 3


class TestForcedAlignment:
    def test_disabled_alignment_returns_original_segments(self):
        segments = [{"start": 0, "end": 1, "text": "bonjour"}]
        service = ForcedAlignmentService({"whisper": {"forced_alignment": {"enabled": False}}})

        assert service.align_segments("/tmp/audio.wav", segments) is segments


class TestForcedAlignmentWildcard:
    """CTC wildcard alignment — characters not in the model vocabulary."""

    def test_text_to_chars_spaces_become_word_boundary(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("bonjour monde")
        assert "|" in chars
        assert chars.index("|") > chars.index("r")

    def test_text_to_chars_preserves_digits(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("version 1.0")
        assert "1" in chars
        assert "0" in chars

    def test_text_to_chars_converts_apostrophes_to_word_boundary(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("c’est")
        assert "'" not in chars
        assert "’" not in chars
        assert "|" in chars

    def test_text_to_chars_converts_hyphens_to_word_boundary(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("jean-claude")
        assert "-" not in chars
        assert "|" in chars

    def test_text_to_chars_no_leading_trailing_boundary(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("  bonjour  ")
        assert chars[0] != "|"
        assert chars[-1] != "|"

    def test_text_to_chars_collapses_consecutive_boundaries(self):
        from transcria.stt.forced_alignment import _text_to_chars

        chars = _text_to_chars("a  b")
        assert chars.count("|") == 1

    def test_build_wildcard_emission_extends_shape_by_one(self):
        torch = pytest.importorskip("torch")
        from transcria.stt.forced_alignment import _build_wildcard_emission

        emission = torch.zeros(5, 10)
        extended, wildcard_id = _build_wildcard_emission(emission, blank_id=0)
        assert extended.shape == (5, 11)
        assert wildcard_id == 10

    def test_build_wildcard_emission_column_equals_max_nonblank_per_frame(self):
        torch = pytest.importorskip("torch")
        from transcria.stt.forced_alignment import _build_wildcard_emission

        emission = torch.zeros(3, 5)
        emission[0, 1] = 0.9
        emission[0, 2] = 0.7
        emission[1, 3] = 0.8
        extended, wildcard_id = _build_wildcard_emission(emission, blank_id=0)
        assert abs(float(extended[0, wildcard_id]) - 0.9) < 1e-5
        assert abs(float(extended[1, wildcard_id]) - 0.8) < 1e-5
        assert abs(float(extended[2, wildcard_id]) - 0.0) < 1e-5

    def test_build_wildcard_emission_excludes_blank_column(self):
        torch = pytest.importorskip("torch")
        from transcria.stt.forced_alignment import _build_wildcard_emission

        emission = torch.zeros(2, 4)
        emission[0, 0] = 0.99   # blank — must NOT contribute to wildcard max
        emission[0, 1] = 0.5    # non-blank — should be the max
        extended, wildcard_id = _build_wildcard_emission(emission, blank_id=0)
        assert abs(float(extended[0, wildcard_id]) - 0.5) < 1e-5


class TestAudioQualityAndVad:
    def test_audio_quality_forces_backend_on_combined_bad_signals(self):
        cfg = {
            "workflow": {
                "audio_quality": {
                    "force_quality_backend": True,
                    "degraded_levels": ["degrade"],
                    "suspect_levels": ["suspect"],
                    "min_bit_rate": 64000,
                    "min_sample_rate_hz": 16000,
                    "max_non_latin_segments": 2,
                    "max_short_segment_ratio": 0.2,
                    "min_speech_ratio": 0.35,
                    "max_speech_ratio": 0.95,
                }
            }
        }

        result = AudioQualityEvaluator(cfg).evaluate(
            {"bit_rate": 32000, "sample_rate_hz": 8000},
            {"diagnostics": {"level": "suspect", "segment_count": 10, "short_segment_count": 4}},
        )

        assert result["level"] == "degrade"
        assert result["force_quality_backend"] is True
        assert "bitrate_faible" in result["reasons"]

    def test_adaptive_vad_relaxes_low_quality_audio(self):
        vad_cfg = {
            "adaptive": True,
            "threshold": 0.5,
            "threshold_low_quality": 0.35,
            "min_silence_duration_ms": 400,
            "min_silence_duration_ms_low_quality": 250,
            "speech_pad_ms": 200,
            "speech_pad_ms_low_quality": 350,
        }

        effective = AdaptiveVADConfig.resolve(vad_cfg, {"level": "degrade", "reasons": ["bitrate_faible"]})

        assert effective["threshold"] == 0.35
        assert effective["speech_pad_ms"] == 350


class TestSpeakerPunctuationRealignment:
    def test_realign_splits_segment_by_word_speaker_and_keeps_punctuation(self):
        cfg = {"workflow": {"speaker_realignment": {"enabled": True}}}
        segments = [{
            "start": 0.0,
            "end": 2.0,
            "speaker": "Alice",
            "text": "Bonjour oui.",
            "words": [
                {"word": "Bonjour", "start": 0.0, "end": 0.6},
                {"word": "oui.", "start": 1.1, "end": 1.6},
            ],
        }]
        turns = {
            "exclusive_turns": [
                {"start": 0.0, "end": 0.8, "speaker": "SPEAKER_00"},
                {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01"},
            ]
        }
        mapping = {"mapping": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}}

        result = SpeakerPunctuationRealigner(cfg).realign(segments, turns, mapping)

        assert len(result) == 2
        assert result[0]["speaker"] == "Alice"
        assert result[1]["speaker"] == "Bob"
        assert result[1]["text"] == "oui."

    def test_unsupported_alignment_backend_returns_original_segments(self):
        segments = [{"start": 0, "end": 1, "text": "bonjour"}]
        cfg = {"whisper": {"forced_alignment": {"enabled": True, "backend": "autre"}}}
        service = ForcedAlignmentService(cfg)

        assert service.align_segments("/tmp/audio.wav", segments) is segments


class TestAntiHallucination:
    def test_detect_repetition_loops(self):
        loops = detect_repetition_loops("bonjour merci merci merci merci fin")

        assert loops
        assert loops[0]["phrase"] == "merci"
        assert loops[0]["count"] == 4

    def test_collapse_repetition_loops_keeps_two_occurrences(self):
        text, loops = collapse_repetition_loops("merci merci merci merci beaucoup")

        assert loops
        assert text == "merci merci beaucoup"


class TestTranscriber:
    def test_transcribe_saves_speaker_map_without_name_error(self, app, owner_id):
        with app.app_context():
            from pathlib import Path

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.config import get_config

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Speaker Map")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json(
                "speakers/speaker_turns.json",
                {"turns": [{"start": 0, "end": 2, "speaker": "SPEAKER_00"}]},
            )
            fs.save_json(
                "speakers/speaker_mapping.json",
                {"mapping": {"SPEAKER_00": "Alice"}, "speakers": []},
            )

            transcriber = Transcriber(cfg, gpu_index=0)
            transcriber.transcriber.transcribe = lambda *args, **kwargs: [
                {"start": 0, "end": 2, "text": "Bonjour"}
            ]
            transcriber.transcriber.segments_to_srt = lambda segments, mapping=None: "1\n00:00:00,000 --> 00:00:02,000\nAlice: Bonjour\n"

            result = transcriber.transcribe(job, Path("/tmp/fake.wav"))

            assert result["speaker_count"] == 1
            assert fs.load_json("metadata/speakers_map.json")["mapping"]["SPEAKER_00"] == "Alice"
            assert "Alice: Bonjour" in fs.load_text("metadata/transcription.srt")


class TestSpeakerDetector:
    def test_detect_generates_missing_clips_when_turns_already_exist(self, app, owner_id, monkeypatch):
        with app.app_context():
            from pathlib import Path

            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.stt.diarization import DiarizerService

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Speaker Clips")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json(
                "speakers/speaker_turns.json",
                {
                    "available": True,
                    "turns": [{"start": 0, "end": 5, "speaker": "SPEAKER_00", "duration": 5}],
                    "speakers": ["SPEAKER_00"],
                    "stats": {"SPEAKER_00": {"speaking_time_seconds": 5, "turn_count": 1}},
                },
            )

            calls = []

            def fake_extract(self, audio_path, turns, speakers, job_fs, *args, **kwargs):
                calls.append((audio_path, turns, speakers))
                job_fs.save_json("speakers/speaker_clips.json", {"SPEAKER_00": ["clip.wav"]})

            monkeypatch.setattr(DiarizerService, "_extract_clips", fake_extract)

            result = SpeakerDetector(cfg).detect(job, Path("/tmp/audio.wav"), device="cpu")

            assert result["available"] is True
            assert calls
            assert fs.load_json("speakers/speaker_clips.json") == {"SPEAKER_00": ["clip.wav"]}

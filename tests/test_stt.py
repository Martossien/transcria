import sys
from types import SimpleNamespace

import pytest

from transcria.audio.vad_adaptive import AdaptiveVADConfig
from transcria.quality.audio_quality import AudioQualityEvaluator
from transcria.stt.anti_hallucination import collapse_repetition_loops, detect_repetition_loops
from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.contextual_biasing import TrieContextualBiasProcessor, build_token_trie, select_lexicon_bias_terms
from transcria.stt.forced_alignment import ForcedAlignmentService
from transcria.stt.granite_transcriber import GraniteTranscriber
from transcria.stt.lexicon_hotwords import build_whisper_hotwords
from transcria.stt.speaker_detection import SpeakerDetector
from transcria.stt.speaker_realignment import SpeakerPunctuationRealigner
from transcria.stt.transcriber_factory import _effective_whisper_config, create_transcriber, get_backend_vram_mb, list_available_backends
from transcria.stt.transcription import Transcriber
from transcria.stt.whisper_transcriber import WhisperTranscriber


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
        numbers = [line for line in lines if line.isdigit()]
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
        assert effective["lexicon_hotwords"]["enabled"] is False

    def test_build_whisper_hotwords_keeps_priority_terms_with_budget(self):
        hotwords, stats = build_whisper_hotwords(
            [
                {"term": "EBITDA", "priority": "critique", "source": "central"},
                {"term": "système de supervision", "priority": "importante", "source": "session"},
                {"term": "terme normal", "priority": "normale", "source": "central"},
                {"term": "ebitda", "priority": "critique", "source": "session"},
            ],
            enabled=True,
            max_terms=2,
            max_chars=80,
            token_counter=lambda text: len(text.split()),
        )

        assert hotwords == "Termes importants : EBITDA, système de supervision"
        assert stats["candidate_terms"] == 4
        assert stats["injected_terms"] == 2
        assert stats["excluded_by_priority"] == 1
        assert stats["excluded_by_duplicate"] == 1
        assert stats["token_count"] == 7
        assert stats["token_count_method"] == "custom"

    def test_build_whisper_hotwords_respects_token_budget(self):
        hotwords, stats = build_whisper_hotwords(
            [
                {"term": "alpha beta", "priority": "critique"},
                {"term": "gamma delta", "priority": "critique"},
            ],
            enabled=True,
            max_terms=10,
            max_chars=200,
            max_tokens=5,
            token_counter=lambda text: len(text.split()),
        )

        assert hotwords == "Termes importants : alpha beta"
        assert stats["injected_terms"] == 1
        assert stats["excluded_by_budget"] == 1
        assert stats["token_count"] == 5

    def test_build_whisper_hotwords_disabled_preserves_static_hotwords(self):
        hotwords, stats = build_whisper_hotwords(
            [{"term": "EBITDA", "priority": "critique"}],
            enabled=False,
            existing_hotwords="Statique",
        )

        assert hotwords == "Statique"
        assert stats["reason"] == "disabled"

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


class TestGraniteTranscriber:
    def test_granite_backend_is_available_in_factory(self):
        assert "granite" in list_available_backends()

    def test_create_granite_transcriber_from_config(self):
        cfg = {
            "models": {"stt_backend": "granite"},
            "granite": {
                "model_id": "./models/granite-speech-4.1-2b",
                "prompt_mode": "asr_punctuated",
            },
        }

        transcriber = create_transcriber(cfg, device="cpu")

        assert isinstance(transcriber, GraniteTranscriber)
        assert transcriber.model_path == "./models/granite-speech-4.1-2b"
        assert transcriber.prompt_mode == "asr_punctuated"

    def test_granite_prompt_uses_fixable_keywords_mode(self):
        transcriber = GraniteTranscriber(
            prompt_mode="keywords",
            keywords=["Terme A", "Acronyme B"],
        )

        prompt = transcriber._build_prompt()

        assert "Keywords: Terme A, Acronyme B" in prompt
        assert prompt.startswith("<|audio|>")

    def test_granite_keywords_mode_falls_back_without_keywords(self):
        transcriber = GraniteTranscriber(prompt_mode="keywords", keywords=[])

        assert transcriber._build_prompt() == transcriber.prompts["asr_punctuated"]

    def test_granite_generation_budget_scales_with_chunk_duration(self):
        transcriber = GraniteTranscriber(
            max_new_tokens=2000,
            max_new_tokens_per_second=8.0,
            min_new_tokens=64,
        )

        assert transcriber._max_new_tokens_for_chunk(5.0) == 64
        assert transcriber._max_new_tokens_for_chunk(30.0) == 240
        assert transcriber._max_new_tokens_for_chunk(300.0) == 2000

    def test_granite_generation_budget_can_disable_scaling(self):
        transcriber = GraniteTranscriber(
            max_new_tokens=2000,
            max_new_tokens_per_second=None,
            min_new_tokens=64,
        )

        assert transcriber._max_new_tokens_for_chunk(5.0) == 2000

    def test_granite_version_tuple_handles_suffixes(self):
        assert GraniteTranscriber._version_tuple("4.57.6") == (4, 57, 6)
        assert GraniteTranscriber._version_tuple("4.52.1.dev0") == (4, 52, 1)

    def test_granite_vram_configurable(self):
        cfg = {"gpu": {"granite_vram_mb": 7200}}

        assert get_backend_vram_mb("granite", cfg) == 7200

    def test_granite_load_retries_without_fix_mistral_regex_when_unsupported(self, monkeypatch):
        calls = []

        class FakeProcessor:
            tokenizer = SimpleNamespace()

            @classmethod
            def from_pretrained(cls, model_path, **kwargs):
                calls.append(kwargs)
                if kwargs.get("fix_mistral_regex"):
                    raise TypeError("unexpected keyword argument 'fix_mistral_regex'")
                return cls()

        class FakeModel:
            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                return cls()

        monkeypatch.setitem(
            sys.modules,
            "transformers",
            SimpleNamespace(
                AutoProcessor=FakeProcessor,
                AutoModelForSpeechSeq2Seq=FakeModel,
            ),
        )
        monkeypatch.setitem(
            sys.modules,
            "torch",
            SimpleNamespace(
                bfloat16="bfloat16",
                float16="float16",
                float32="float32",
            ),
        )
        monkeypatch.setattr(GraniteTranscriber, "available", property(lambda self: True))

        transcriber = GraniteTranscriber(model_path="/tmp/model", device="cpu", fix_mistral_regex=True)

        assert transcriber.load()
        assert calls[0]["fix_mistral_regex"] is True
        assert "fix_mistral_regex" not in calls[1]
        assert transcriber.get_metadata()["fix_mistral_regex"] is False


class TestContextualBiasing:
    def test_select_lexicon_bias_terms_keeps_validated_targets_only(self):
        terms, stats = select_lexicon_bias_terms(
            [
                {"term": "indemnités", "priority": "critique", "variants": ["inimités"]},
                {"term": "DIF", "priority": "importante"},
                {"term": "terme normal", "priority": "normale"},
                {"term": "dif", "priority": "critique"},
            ],
            enabled=True,
            priorities=["critique", "importante"],
            max_terms=10,
        )

        assert terms == ["indemnités", "DIF"]
        assert "inimités" not in terms
        assert stats["candidate_terms"] == 4
        assert stats["excluded_by_priority"] == 1
        assert stats["excluded_by_duplicate"] == 1

    def test_trie_contextual_bias_processor_boosts_only_continuation(self):
        torch = pytest.importorskip("torch")

        class Tokenizer:
            def encode(self, text, add_special_tokens=False):
                mapping = {" alpha": [10], "alpha": [10], " beta": [11], "beta": [11], " alpha beta": [10, 11], "alpha beta": [10, 11]}
                return mapping[text]

        root, stats = build_token_trie(["alpha beta"], Tokenizer())
        processor = TrieContextualBiasProcessor(root, boost=0.2, max_prefix_tokens=5)
        input_ids = torch.tensor([[10], [99]])
        scores = torch.zeros((2, 20), dtype=torch.float32)

        output = processor(input_ids, scores)

        assert stats["token_sequences"] == 1
        assert output[0, 11].item() > 0
        assert output[1, 11].item() == 0


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

    def test_audio_quality_records_scene_findings_without_forcing_by_default(self):
        cfg = {
            "workflow": {
                "audio_quality": {
                    "force_quality_backend": True,
                    "scene_affects_quality_score": False,
                    "max_scene_music_ratio": 0.15,
                    "max_scene_noise_ratio": 0.20,
                    "max_scene_no_energy_ratio": 0.30,
                    "min_scene_speech_ratio": 0.55,
                    "max_scene_problem_segments": 1,
                }
            }
        }

        result = AudioQualityEvaluator(cfg).evaluate(
            {},
            {"diagnostics": {}},
            audio_scene={
                "has_music": True,
                "has_noise": True,
                "speech_ratio": 0.5,
                "music_ratio": 0.2,
                "noise_ratio": 0.25,
                "no_energy_ratio": 0.35,
                "non_speech_ratio": 0.5,
                "problem_segments": [{"label": "noise"}, {"label": "music"}],
            },
        )

        assert result["level"] == "ok"
        assert result["score"] == 0
        assert result["force_quality_backend"] is False
        assert "scene_musique_importante" in result["scene_findings"]
        assert "scene_bruit_important" in result["scene_findings"]
        assert result["scene_metrics"]["problem_segment_count"] == 2

    def test_audio_quality_can_apply_scene_score_when_enabled(self):
        cfg = {
            "workflow": {
                "audio_quality": {
                    "force_quality_backend": True,
                    "scene_affects_quality_score": True,
                    "max_scene_noise_ratio": 0.20,
                    "min_scene_speech_ratio": 0.55,
                }
            }
        }

        result = AudioQualityEvaluator(cfg).evaluate(
            {},
            {"diagnostics": {}},
            audio_scene={
                "has_noise": True,
                "speech_ratio": 0.4,
                "noise_ratio": 0.3,
            },
        )

        assert result["level"] == "degrade"
        assert result["force_quality_backend"] is True
        assert "scene_bruit_important" in result["reasons"]
        assert "scene_parole_faible" in result["reasons"]

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
    def _transcriber_for_cleanup(self, cleanup_cfg=None):
        transcriber = Transcriber.__new__(Transcriber)
        transcriber.config = {
            "workflow": {
                "transcription_cleanup": cleanup_cfg or {
                    "enabled": True,
                    "remove_subtitle_artifacts": True,
                    "merge_short_segments": True,
                    "short_segment_max_s": 0.45,
                    "short_segment_max_words": 2,
                    "merge_gap_s": 0.5,
                    "merge_max_chars": 220,
                }
            }
        }
        return transcriber

    def test_cleanup_removes_known_subtitle_artifacts(self):
        transcriber = self._transcriber_for_cleanup()
        segments = [
            {"start": 0.0, "end": 0.2, "speaker": "Alice", "text": "Sous"},
            {"start": 0.3, "end": 0.8, "speaker": "Alice", "text": "-titrage ST' 501"},
            {"start": 1.0, "end": 1.8, "speaker": "Alice", "text": "Bonjour à tous."},
            {"start": 2.0, "end": 2.5, "speaker": "Alice", "text": "Société Radio-Canada"},
        ]

        cleaned = transcriber._cleanup_transcription_segments(segments)

        assert [s["text"] for s in cleaned] == ["Bonjour à tous."]

    def test_cleanup_removes_truncated_subtitle_artifacts(self):
        transcriber = self._transcriber_for_cleanup()
        segments = [
            {"start": 0.0, "end": 0.4, "speaker": "Alice", "text": "-titrage FR?"},
            {"start": 0.5, "end": 0.9, "speaker": "Alice", "text": "-titrage ST'"},
            {"start": 1.0, "end": 2.0, "speaker": "Alice", "text": "Point suivant."},
        ]

        cleaned = transcriber._cleanup_transcription_segments(segments)

        assert [s["text"] for s in cleaned] == ["Point suivant."]

    def test_cleanup_merges_short_same_speaker_segments(self):
        transcriber = self._transcriber_for_cleanup()
        segments = [
            {"start": 0.0, "end": 1.0, "speaker": "Alice", "text": "Nous ouvrons la séance."},
            {"start": 1.2, "end": 1.4, "speaker": "Alice", "text": "Oui"},
            {"start": 2.0, "end": 2.2, "speaker": "Bob", "text": "Non"},
        ]

        cleaned = transcriber._cleanup_transcription_segments(segments)

        assert cleaned[0]["text"] == "Nous ouvrons la séance. Oui"
        assert cleaned[0]["end"] == 1.4
        assert cleaned[1]["text"] == "Non"

    def test_cleanup_keeps_long_sentences_with_artifact_words(self):
        transcriber = self._transcriber_for_cleanup()
        segments = [
            {
                "start": 0.0,
                "end": 3.0,
                "speaker": "Alice",
                "text": "La convention avec Société Radio-Canada doit être relue.",
            },
        ]

        cleaned = transcriber._cleanup_transcription_segments(segments)

        assert cleaned == segments

    def test_cleanup_can_be_disabled(self):
        transcriber = self._transcriber_for_cleanup({"enabled": False})
        segments = [{"start": 0.0, "end": 0.2, "speaker": "Alice", "text": "Sous"}]

        assert transcriber._cleanup_transcription_segments(segments) == segments

    def test_segment_reliability_marks_degraded_when_audio_and_confidence_are_bad(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        segments = [{
            "start": 0.0,
            "end": 0.3,
            "text": "La médecine",
            "no_speech_prob": 0.62,
            "words": [
                {"word": "La", "probability": 0.03},
                {"word": "médecine", "probability": 0.5},
            ],
        }]

        scored = SegmentReliabilityScorer({}).score_segments(
            segments,
            {"flags": ["audio_tres_faible", "risque_transcription_non_fiable"]},
        )

        assert scored[0]["text"] == "La médecine"
        assert scored[0]["reliability"] == "degrade"
        assert "audio_preflight_degrade" in scored[0]["reliability_reasons"]
        assert "no_speech_prob_eleve" in scored[0]["reliability_reasons"]

    def test_segment_reliability_flags_configured_generic_hallucination(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        config = {
            "workflow": {
                "segment_reliability": {
                    "detect_generic_hallucinations": True,
                    "generic_hallucination_patterns": [r"\bsite web\b"],
                    "degrade_on_text_flags": True,
                }
            }
        }
        segments = [{"start": 12.0, "end": 16.0, "text": "Retrouvez les détails sur notre site web."}]

        scored = SegmentReliabilityScorer(config).score_segments(segments)

        assert scored[0]["reliability"] == "degrade"
        assert scored[0]["reliability_reasons"] == ["hallucination_generique"]

    def test_segment_reliability_flags_short_english_generic_hallucination(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        config = {
            "workflow": {
                "segment_reliability": {
                    "detect_generic_hallucinations": True,
                    "generic_hallucination_patterns": [
                        r"^\s*thank\s+you\s+very\s+much\s*[.!?…]*\s*$",
                    ],
                    "degrade_on_text_flags": True,
                }
            }
        }
        segments = [{"start": 0.0, "end": 30.0, "text": "Thank you very much."}]

        scored = SegmentReliabilityScorer(config).score_segments(segments)

        assert scored[0]["reliability"] == "degrade"
        assert scored[0]["reliability_reasons"] == ["hallucination_generique"]

    def test_segment_reliability_does_not_flag_thank_you_inside_real_sentence(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        config = {
            "workflow": {
                "segment_reliability": {
                    "detect_generic_hallucinations": True,
                    "generic_hallucination_patterns": [
                        r"^\s*thank\s+you\s*[.!?…]*\s*$",
                    ],
                    "degrade_on_text_flags": True,
                }
            }
        }
        segments = [{"start": 0.0, "end": 4.0, "text": "I said thank you before leaving."}]

        scored = SegmentReliabilityScorer(config).score_segments(segments)

        assert scored[0]["reliability"] == "ok"
        assert scored[0]["reliability_reasons"] == []

    def test_segment_reliability_flags_configured_non_latin_pattern(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        config = {
            "workflow": {
                "segment_reliability": {
                    "detect_non_latin": True,
                    "non_latin_char_pattern": "[\\u4E00-\\u9FFF]",
                    "non_latin_min_chars": 2,
                    "degrade_on_text_flags": True,
                }
            }
        }
        segments = [{"start": 12.0, "end": 16.0, "text": "Phrase normale 不明白"}]

        scored = SegmentReliabilityScorer(config).score_segments(segments)

        assert scored[0]["reliability"] == "degrade"
        assert scored[0]["reliability_reasons"] == ["texte_non_latin"]

    def test_segment_reliability_ignores_generic_hallucinations_without_configured_patterns(self):
        from transcria.stt.reliability import SegmentReliabilityScorer

        segments = [{"start": 12.0, "end": 16.0, "text": "Retrouvez les détails sur notre site web."}]

        scored = SegmentReliabilityScorer({}).score_segments(segments)

        assert scored[0]["reliability"] == "ok"
        assert scored[0]["reliability_reasons"] == []

    def test_smooth_micro_turns_merges_same_speaker_only(self):
        turns = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_00", "start": 1.1, "end": 1.2},
            {"speaker": "SPEAKER_01", "start": 1.25, "end": 1.32},
        ]

        smoothed = Transcriber._smooth_micro_turns(turns, {
            "merge_micro_chunks": True,
            "micro_chunk_s": 0.35,
            "micro_chunk_neighbor_gap_s": 0.4,
        })

        assert len(smoothed) == 2
        assert smoothed[0]["end"] == pytest.approx(1.2)
        assert smoothed[1]["speaker"] == "SPEAKER_01"

    def test_final_vad_auto_enables_on_degraded_audio(self):
        vad_cfg = {
            "enabled_final": False,
            "auto_enable_final_on_degraded": True,
            "auto_enable_final_levels": ["degrade"],
            "threshold": 0.35,
            "threshold_final_degraded": 0.6,
        }

        enabled = Transcriber._resolve_final_vad_enabled(
            vad_cfg,
            {"level": "degrade"},
            {"enable_vad": True},
        )

        assert enabled is True
        assert vad_cfg["threshold"] == 0.6

    def test_transcribe_saves_speaker_map_without_name_error(self, app, owner_id):
        with app.app_context():
            from pathlib import Path

            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore

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
            transcriber.transcriber.get_metadata = lambda: {"backend": "cohere", "calls": 1}

            result = transcriber.transcribe(job, Path("/tmp/fake.wav"))

            assert result["speaker_count"] == 1
            assert fs.load_json("metadata/speakers_map.json")["mapping"]["SPEAKER_00"] == "Alice"
            assert "Alice: Bonjour" in fs.load_text("metadata/transcription.srt")
            assert fs.load_json("metadata/cohere.json")["calls"] == 1
            assert fs.load_json("metadata/transcription_metadata.json")["backend_metadata_path"] == "metadata/cohere.json"


class TestTranscriberConfigurableArtifacts:
    """Point 1 — marqueurs d'artefacts configurables depuis config.yaml."""

    def _transcriber(self, cleanup_cfg: dict):
        t = Transcriber.__new__(Transcriber)
        t.config = {"workflow": {"transcription_cleanup": cleanup_cfg}}
        return t

    def _base_cleanup(self, extra: dict | None = None) -> dict:
        cfg = {
            "enabled": True,
            "remove_subtitle_artifacts": True,
            "merge_short_segments": False,
        }
        if extra:
            cfg.update(extra)
        return cfg

    def test_custom_pattern_from_config_filters_matching_segment(self):
        """Un pattern regex personnalisé dans config filtre le segment correspondant."""
        t = self._transcriber(self._base_cleanup({
            "subtitle_artifact_patterns": [r"\bcustom_watermark\b"],
            "subtitle_artifact_words": [],
        }))
        segments = [
            {"start": 0.0, "end": 0.3, "text": "custom_watermark"},
            {"start": 1.0, "end": 2.0, "text": "Bonjour à tous."},
        ]

        cleaned = t._cleanup_transcription_segments(segments)

        assert [s["text"] for s in cleaned] == ["Bonjour à tous."]

    def test_custom_word_from_config_filters_matching_segment(self):
        """Un mot court personnalisé dans config filtre le segment correspondant."""
        t = self._transcriber(self._base_cleanup({
            "subtitle_artifact_patterns": [],
            "subtitle_artifact_words": ["custom_noise"],
        }))
        segments = [
            {"start": 0.0, "end": 0.3, "text": "custom_noise"},
            {"start": 1.0, "end": 2.0, "text": "Point suivant."},
        ]

        cleaned = t._cleanup_transcription_segments(segments)

        assert [s["text"] for s in cleaned] == ["Point suivant."]

    def test_custom_patterns_replace_defaults_builtin_no_longer_filtered(self):
        """Quand des patterns custom sont fournis, les défauts intégrés ne s'appliquent plus."""
        t = self._transcriber(self._base_cleanup({
            "subtitle_artifact_patterns": [r"\bcustom_watermark\b"],
            "subtitle_artifact_words": ["custom_noise"],
        }))
        segments = [
            # Artefacts intégrés — ne doivent PAS être filtrés (défauts remplacés)
            {"start": 0.0, "end": 0.3, "text": "Sous-titrage ST' 501"},
            {"start": 0.4, "end": 0.6, "text": "titrage"},
            # Artefact custom — doit être filtré
            {"start": 0.7, "end": 0.9, "text": "custom_watermark"},
            {"start": 1.0, "end": 2.0, "text": "Conclusion."},
        ]

        cleaned = t._cleanup_transcription_segments(segments)

        texts = [s["text"] for s in cleaned]
        assert "Sous-titrage ST' 501" in texts
        assert "titrage" in texts
        assert "custom_watermark" not in texts
        assert "Conclusion." in texts

    def test_empty_lists_in_config_use_builtin_defaults(self):
        """Des listes vides dans config activent les défauts intégrés."""
        t = self._transcriber(self._base_cleanup({
            "subtitle_artifact_patterns": [],
            "subtitle_artifact_words": [],
        }))
        segments = [
            {"start": 0.0, "end": 0.3, "text": "Sous-titrage ST' 501"},
            {"start": 0.4, "end": 0.6, "text": "titrage"},
            {"start": 1.0, "end": 2.0, "text": "Bonjour."},
        ]

        cleaned = t._cleanup_transcription_segments(segments)

        texts = [s["text"] for s in cleaned]
        assert "Sous-titrage ST' 501" not in texts
        assert "titrage" not in texts
        assert "Bonjour." in texts

    def test_absent_keys_in_config_also_use_builtin_defaults(self):
        """Clés absentes de config (pas de subtitle_artifact_*) → défauts intégrés."""
        t = self._transcriber(self._base_cleanup())  # pas de subtitle_artifact_*
        segments = [
            {"start": 0.0, "end": 0.5, "text": "Société Radio-Canada"},
            {"start": 1.0, "end": 2.0, "text": "Ordre du jour."},
        ]

        cleaned = t._cleanup_transcription_segments(segments)

        texts = [s["text"] for s in cleaned]
        assert "Société Radio-Canada" not in texts
        assert "Ordre du jour." in texts


class TestCohereAntiHallucination:
    """Point 2 — anti-hallucination post-inférence pour CohereTranscriber."""

    def test_apply_loop_collapse_reduces_repetition(self):
        """_apply_loop_collapse réduit une boucle répétitive à keep_repeats occurrences."""
        ct = CohereTranscriber(
            collapse_repetition_loops=True,
            repetition_loop_min_repeats=4,
            repetition_loop_keep_repeats=2,
        )
        text = "voici voici voici voici voici la fin"

        result, loops = ct._apply_loop_collapse(text)

        assert loops
        assert "voici voici voici voici voici" not in result
        assert result == "voici voici la fin"

    def test_apply_loop_collapse_returns_loop_metadata(self):
        """La liste loops contient phrase, count pour chaque boucle détectée."""
        ct = CohereTranscriber(
            collapse_repetition_loops=True,
            repetition_loop_min_repeats=4,
        )
        _, loops = ct._apply_loop_collapse("merci merci merci merci merci beaucoup")

        assert loops
        assert loops[0]["phrase"] == "merci"
        assert loops[0]["count"] == 5

    def test_apply_loop_collapse_disabled_returns_text_unchanged(self):
        """Quand collapse_repetition_loops=False, texte et liste loops sont inchangés."""
        ct = CohereTranscriber(collapse_repetition_loops=False)
        text = "merci merci merci merci merci"

        result, loops = ct._apply_loop_collapse(text)

        assert result == text
        assert loops == []

    def test_apply_loop_collapse_normal_text_unaffected(self):
        """Un texte sans répétition ressort identique, loops vide."""
        ct = CohereTranscriber(collapse_repetition_loops=True)
        text = "Bonjour à tous, je vous remercie de votre présence."

        result, loops = ct._apply_loop_collapse(text)

        assert result == text
        assert loops == []

    def test_transcribe_with_fake_model_collapses_loops_in_output(self):
        """Le pipeline transcribe() intègre la détection de boucles sur chaque segment."""
        import numpy as np

        class FakeProcessor:
            def __call__(self, audio, sampling_rate, return_tensors, language):
                import types
                ns = types.SimpleNamespace(
                    dtype=type("DT", (), {"__eq__": lambda s, o: False})(),
                    __getitem__=lambda s, k: s,
                )
                ns.to = lambda dtype: ns
                return {"input_features": ns}
            def decode(self, ids, skip_special_tokens):
                return "bonjour bonjour bonjour bonjour bonjour fin"

        class FakeModel:
            def generate(self, *args, **kwargs):
                return [[1, 2, 3]]
            def to(self, *a, **k):
                return self

        ct = CohereTranscriber(
            collapse_repetition_loops=True,
            repetition_loop_min_repeats=4,
            repetition_loop_keep_repeats=1,
        )
        ct._model = FakeModel()
        ct._processor = FakeProcessor()

        audio = np.zeros(16000, dtype=np.float32)
        segments = ct.transcribe(audio_path=None, audio_array=audio, sample_rate=16000)

        assert segments
        seg = segments[0]
        assert "bonjour bonjour bonjour bonjour bonjour" not in seg["text"]
        assert "hallucination_loops" in seg
        assert seg["hallucination_loops"][0]["phrase"] == "bonjour"


class TestDefaultSubtitleArtifactPatterns:
    """Patterns outro YouTube et crédits tiers présents dans les défauts intégrés."""

    def _run(self, *texts):
        """Lance le nettoyage sur une liste de segments avec la config par défaut (pas de clés custom)."""
        t = Transcriber.__new__(Transcriber)
        t.config = {"workflow": {"transcription_cleanup": {
            "enabled": True,
            "remove_subtitle_artifacts": True,
            "merge_short_segments": False,
        }}}
        segs = [{"start": i * 1.0, "end": i * 1.0 + 0.3, "text": txt} for i, txt in enumerate(texts)]
        return [s["text"] for s in t._cleanup_transcription_segments(segs)]

    def test_thanks_for_watching_removed(self):
        """Phrase outro YouTube anglaise classique → supprimée par les défauts."""
        result = self._run("Thanks for watching", "Bonjour.")
        assert "Thanks for watching" not in result
        assert "Bonjour." in result

    def test_thank_you_for_watching_removed(self):
        """Variante longue 'thank you for watching' → supprimée."""
        result = self._run("Thank you for watching.", "Bonjour.")
        assert "Thank you for watching." not in result

    def test_please_subscribe_to_my_channel_removed(self):
        """Appel à l'abonnement YouTube → supprimé."""
        result = self._run("Please subscribe to my channel", "Bonjour.")
        assert "Please subscribe to my channel" not in result

    def test_like_and_subscribe_removed(self):
        """Phrase d'appel à l'engagement YouTube → supprimée."""
        result = self._run("like and subscribe", "Bonjour.")
        assert "like and subscribe" not in result

    def test_amara_subtitles_credit_removed(self):
        """Crédit sous-titrage Amara.org → supprimé."""
        result = self._run("Subtitles by the Amara.org community", "Bonjour.")
        assert "Subtitles by the Amara.org community" not in result

    def test_thank_you_alone_not_removed(self):
        """'thank you' seul reste conservable si le filtre hallucination est désactivé."""
        t = Transcriber.__new__(Transcriber)
        t.config = {"workflow": {"transcription_cleanup": {
            "enabled": True,
            "remove_subtitle_artifacts": True,
            "remove_obvious_hallucinations": False,
            "merge_short_segments": False,
        }}}
        segs = [
            {"start": 0.0, "end": 0.3, "text": "thank you"},
            {"start": 1.0, "end": 1.3, "text": "Bonjour."},
        ]
        result = [s["text"] for s in t._cleanup_transcription_segments(segs)]
        assert "thank you" in result

    def test_short_legitimate_french_word_not_removed(self):
        """Mot français courant court n'est PAS supprimé."""
        result = self._run("Merci.", "Bonjour.")
        assert "Merci." in result

    def test_non_latin_hallucination_removed(self):
        """Un segment majoritairement arabe/japonais sur réunion FR est supprimé."""
        result = self._run("شكرا جزيلا", "Bonjour.")
        assert "شكرا جزيلا" not in result
        assert "Bonjour." in result

    def test_mixed_french_with_small_non_latin_fragment_kept(self):
        """Un texte français contenant un faible fragment non latin n'est pas supprimé."""
        result = self._run("Bonjour 漢字 dossier.", "Point suivant.")
        assert "Bonjour 漢字 dossier." in result

    def test_standalone_thank_you_removed_for_french_cleanup(self):
        """Sur transcription FR, 'thank you' isolé est une hallucination générique mesurée."""
        result = self._run("thank you", "Bonjour.")
        assert "thank you" not in result
        assert "Bonjour." in result

    def test_standalone_thank_you_kept_for_english_job(self):
        """Sur job anglais explicite, le filtre générique français ne s'applique pas."""
        t = Transcriber.__new__(Transcriber)
        t.config = {"workflow": {"transcription_cleanup": {
            "enabled": True,
            "remove_subtitle_artifacts": True,
            "remove_obvious_hallucinations": True,
            "merge_short_segments": False,
        }}}
        segs = [
            {"start": 0.0, "end": 0.3, "text": "thank you"},
            {"start": 1.0, "end": 1.3, "text": "Next point."},
        ]
        result = [s["text"] for s in t._cleanup_transcription_segments(segs, language="en")]
        assert "thank you" in result


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

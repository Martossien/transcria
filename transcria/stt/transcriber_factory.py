import logging

from transcria.config.loader import _deep_merge, get_default_config
from transcria.stt.base_transcriber import BaseTranscriber

logger = logging.getLogger(__name__)

_STT_BACKENDS = ("cohere", "whisper", "granite", "parakeet")


def create_transcriber(
    config: dict,
    backend: str | None = None,
    device: str | None = None,
) -> BaseTranscriber:

    if backend is None:
        backend = config.get("models", {}).get("stt_backend", "cohere")

    if _should_use_remote_stt(config, backend):
        from transcria.stt.remote_transcriber import RemoteTranscriber

        logger.info("Transcription : backend distant '%s' (inference.stt)", backend)
        return RemoteTranscriber(config, backend=backend, device=device)

    if backend not in _STT_BACKENDS:
        logger.warning(
            "Backend STT inconnu '%s', fallback sur cohere. Backends disponibles: %s",
            backend,
            _STT_BACKENDS,
        )
        backend = "cohere"

    if backend == "cohere":
        return _create_cohere(config, device)
    elif backend == "whisper":
        return _create_whisper(config, device)
    elif backend == "granite":
        return _create_granite(config, device)
    elif backend == "parakeet":
        return _create_parakeet(config, device)

    return _create_cohere(config, device)


def _create_cohere(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.cohere_transcriber import CohereTranscriber

    models_cfg = config.get("models", {})
    cohere_cfg = config.get("cohere", {})
    lexicon_biasing_cfg = cohere_cfg.get("lexicon_biasing", {})
    if not isinstance(lexicon_biasing_cfg, dict):
        lexicon_biasing_cfg = {}

    return CohereTranscriber(
        model_path=models_cfg.get("cohere_model_path"),
        model_revision=models_cfg.get("cohere_model_revision"),
        device=device,
        chunk_length_s=cohere_cfg.get("chunk_length_s", 30),
        max_new_tokens=cohere_cfg.get("max_new_tokens", 448),
        punctuation=cohere_cfg.get("punctuation", True),
        repetition_penalty=cohere_cfg.get("repetition_penalty", 1.2),
        no_repeat_ngram_size=cohere_cfg.get("no_repeat_ngram_size", 4),
        collapse_repetition_loops=cohere_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=cohere_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=cohere_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=cohere_cfg.get("repetition_loop_keep_repeats", 2),
        lexicon_biasing_enabled=lexicon_biasing_cfg.get("enabled", False),
        lexicon_biasing_terms=cohere_cfg.get("_lexicon_bias_terms", []),
        lexicon_biasing_boost=lexicon_biasing_cfg.get("boost", 0.2),
        lexicon_biasing_start_boost=lexicon_biasing_cfg.get("start_boost", 0.05),
        lexicon_biasing_max_prefix_tokens=lexicon_biasing_cfg.get("max_prefix_tokens", 20),
    )


def _create_whisper(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.whisper_transcriber import WhisperTranscriber

    whisper_cfg = _effective_whisper_config(config)

    return WhisperTranscriber(
        model_size=whisper_cfg["model_size"],
        device=device,
        compute_type=whisper_cfg["compute_type"],
        cpu_threads=whisper_cfg["cpu_threads"],
        chunk_length_s=whisper_cfg["chunk_length_s"],
        beam_size=whisper_cfg["beam_size"],
        best_of=whisper_cfg["best_of"],
        vad_filter=whisper_cfg["vad_filter"],
        word_timestamps=whisper_cfg["word_timestamps"],
        condition_on_previous_text=whisper_cfg["condition_on_previous_text"],
        no_speech_threshold=whisper_cfg["no_speech_threshold"],
        compression_ratio_threshold=whisper_cfg["compression_ratio_threshold"],
        log_prob_threshold=whisper_cfg["log_prob_threshold"],
        hallucination_silence_threshold=whisper_cfg["hallucination_silence_threshold"],
        repetition_penalty=whisper_cfg["repetition_penalty"],
        no_repeat_ngram_size=whisper_cfg["no_repeat_ngram_size"],
        suppress_numerals=whisper_cfg["suppress_numerals"],
        hotwords=whisper_cfg.get("hotwords"),
        initial_prompt=whisper_cfg.get("initial_prompt"),
        collapse_repetition_loops=whisper_cfg["collapse_repetition_loops"],
        repetition_loop_min_repeats=whisper_cfg["repetition_loop_min_repeats"],
        repetition_loop_max_phrase_words=whisper_cfg["repetition_loop_max_phrase_words"],
        repetition_loop_keep_repeats=whisper_cfg["repetition_loop_keep_repeats"],
    )


def _create_granite(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.granite_transcriber import GraniteTranscriber

    granite_cfg = _effective_granite_config(config)
    return GraniteTranscriber(
        model_path=granite_cfg.get("model_id"),
        device=device,
        chunk_length_s=granite_cfg.get("chunk_length_s", 300),
        max_new_tokens=granite_cfg.get("max_new_tokens", 2000),
        max_new_tokens_per_second=granite_cfg.get("max_new_tokens_per_second", 8.0),
        min_new_tokens=granite_cfg.get("min_new_tokens", 64),
        torch_dtype=granite_cfg.get("torch_dtype", "bfloat16"),
        prompt_mode=granite_cfg.get("prompt_mode", "asr_punctuated"),
        prompt_asr_raw=granite_cfg.get("prompt_asr_raw"),
        prompt_asr_punctuated=granite_cfg.get("prompt_asr_punctuated"),
        prompt_keywords=granite_cfg.get("prompt_keywords"),
        keywords=granite_cfg.get("keywords"),
        fix_mistral_regex=granite_cfg.get("fix_mistral_regex", True),
        collapse_repetition_loops=granite_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=granite_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=granite_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=granite_cfg.get("repetition_loop_keep_repeats", 2),
    )


def _effective_whisper_config(config: dict) -> dict:
    legacy = config.get("models", {}).get("whisper", {})
    current = config.get("whisper", {})
    defaults = get_default_config()["whisper"]
    return _deep_merge(_deep_merge(defaults, legacy), current)


def _effective_granite_config(config: dict) -> dict:
    current = config.get("granite", {})
    defaults = get_default_config()["granite"]
    return _deep_merge(defaults, current)


def _create_parakeet(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.parakeet_transcriber import ParakeetTranscriber

    parakeet_cfg = _effective_parakeet_config(config)
    att_ctx = parakeet_cfg.get("att_context_size", [256, 256])
    return ParakeetTranscriber(
        model_path=parakeet_cfg.get("model_id"),
        device=device,
        use_local_attention=parakeet_cfg.get("use_local_attention", True),
        att_context_size=(int(att_ctx[0]), int(att_ctx[1])),
        decoding_strategy=parakeet_cfg.get("decoding_strategy", "greedy_batch"),
        decoding_beam_size=parakeet_cfg.get("decoding_beam_size", 2),
        max_chunk_duration_s=parakeet_cfg.get("max_chunk_duration_s", 1200),
        collapse_repetition_loops=parakeet_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=parakeet_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=parakeet_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=parakeet_cfg.get("repetition_loop_keep_repeats", 2),
    )


def _effective_parakeet_config(config: dict) -> dict:
    current = config.get("parakeet", {})
    defaults = get_default_config()["parakeet"]
    return _deep_merge(defaults, current)


def _should_use_remote_stt(config: dict, backend: str) -> bool:
    """True si le STT doit passer par un serveur vLLM distant pour ce backend.

    Conditions : `inference.mode` ∈ {remote, hybrid} ET un endpoint est configuré
    pour ce backend dans `inference.stt.backends`. Sinon, transcription locale
    (comportement historique préservé, y compris pour granite/parakeet non mappés).
    """
    inf = config.get("inference", {}) or {}
    if inf.get("mode", "local") not in ("remote", "hybrid"):
        return False
    backends = ((inf.get("stt", {}) or {}).get("backends", {}) or {})
    return bool((backends.get(backend, {}) or {}).get("url"))


def list_available_backends() -> list[str]:
    return list(_STT_BACKENDS)


def get_backend_vram_mb(backend: str, config: dict) -> int:
    if backend == "cohere":
        return int(config.get("gpu", {}).get("cohere_vram_mb", get_default_config()["gpu"]["cohere_vram_mb"]))
    elif backend == "whisper":
        from transcria.stt.whisper_transcriber import WhisperTranscriber

        whisper_cfg = _effective_whisper_config(config)
        size = whisper_cfg["model_size"]
        return WhisperTranscriber.vram_for_size(size)
    elif backend == "granite":
        return int(config.get("gpu", {}).get("granite_vram_mb", get_default_config()["gpu"]["granite_vram_mb"]))
    elif backend == "parakeet":
        return int(config.get("gpu", {}).get("parakeet_vram_mb", get_default_config()["gpu"]["parakeet_vram_mb"]))
    return int(config.get("gpu", {}).get("cohere_vram_mb", get_default_config()["gpu"]["cohere_vram_mb"]))

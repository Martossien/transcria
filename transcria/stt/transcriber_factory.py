import logging
from pathlib import Path

from transcria.config.loader import _deep_merge, get_default_config
from transcria.stt.base_transcriber import BaseTranscriber

logger = logging.getLogger(__name__)

_STT_BACKENDS = ("cohere", "whisper")


def create_transcriber(
    config: dict,
    backend: str | None = None,
    device: str | None = None,
) -> BaseTranscriber:

    if backend is None:
        backend = config.get("models", {}).get("stt_backend", "cohere")

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

    return _create_cohere(config, device)


def _create_cohere(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.cohere_transcriber import CohereTranscriber

    models_cfg = config.get("models", {})
    cohere_cfg = config.get("cohere", {})

    return CohereTranscriber(
        model_path=models_cfg.get("cohere_model_path"),
        device=device,
        chunk_length_s=cohere_cfg.get("chunk_length_s", 30),
        max_new_tokens=cohere_cfg.get("max_new_tokens", 448),
        repetition_penalty=cohere_cfg.get("repetition_penalty", 1.2),
        no_repeat_ngram_size=cohere_cfg.get("no_repeat_ngram_size", 3),
        collapse_repetition_loops=cohere_cfg.get("collapse_repetition_loops", True),
        repetition_loop_min_repeats=cohere_cfg.get("repetition_loop_min_repeats", 4),
        repetition_loop_max_phrase_words=cohere_cfg.get("repetition_loop_max_phrase_words", 10),
        repetition_loop_keep_repeats=cohere_cfg.get("repetition_loop_keep_repeats", 2),
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


def _effective_whisper_config(config: dict) -> dict:
    legacy = config.get("models", {}).get("whisper", {})
    current = config.get("whisper", {})
    defaults = get_default_config()["whisper"]
    return _deep_merge(_deep_merge(defaults, legacy), current)


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
    return int(config.get("gpu", {}).get("cohere_vram_mb", get_default_config()["gpu"]["cohere_vram_mb"]))

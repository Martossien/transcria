import logging
from pathlib import Path

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
    )


def _create_whisper(config: dict, device: str | None) -> BaseTranscriber:
    from transcria.stt.whisper_transcriber import WhisperTranscriber

    whisper_cfg = config.get("whisper", {}) or config.get("models", {}).get("whisper", {})

    if device and device.startswith("cuda"):
        device = "cuda"

    return WhisperTranscriber(
        model_size=whisper_cfg.get("model_size", "large-v3"),
        device=device,
        compute_type=whisper_cfg.get("compute_type", "int8"),
        cpu_threads=whisper_cfg.get("cpu_threads", 4),
        chunk_length_s=whisper_cfg.get("chunk_length_s", 30),
        beam_size=whisper_cfg.get("beam_size", 5),
        best_of=whisper_cfg.get("best_of", 5),
        vad_filter=whisper_cfg.get("vad_filter", True),
    )


def list_available_backends() -> list[str]:
    return list(_STT_BACKENDS)


def get_backend_vram_mb(backend: str, config: dict) -> int:
    if backend == "cohere":
        return 6000
    elif backend == "whisper":
        from transcria.stt.whisper_transcriber import WhisperTranscriber

        whisper_cfg = config.get("whisper", {}) or config.get("models", {}).get("whisper", {})
        size = whisper_cfg.get("model_size", "large-v3")
        return WhisperTranscriber.vram_for_size(size)
    return 6000

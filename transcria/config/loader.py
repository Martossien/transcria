import copy
import os
import yaml


_DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 7870, "debug": True},
    "storage": {"jobs_dir": "./jobs", "database_url": "sqlite:///transcrIA.db"},
    "auth": {"enabled": True, "first_admin_username": "admin", "first_admin_password": "CHANGE-ME"},
    "gpu": {
        "cohere_vram_mb": 6000,
        "pyannote_vram_mb": 2000,
        "llm_vram_mb": 60000,
        "min_free_vram_mb": 4000,
    },
    "services": {
        "dashboard_llm_url": "http://127.0.0.1:5001",
        "srt_editor_easy_url": "http://127.0.0.1:7861",
        "arbitrage_script": "./scripts/launch_arbitrage.sh",
        "stop_script": "./scripts/stop_arbitrage_llm.sh",
        "arbitrage_llm_port": 8080,
        "llm_cleanup_ports": [8000],
    },
    "models": {
        "stt_backend": "cohere",
        "default_stt_model": "cohere-transcribe-03-2026",
        "fallback_stt_model": "large-v3",
        "cohere_model_path": "./models/cohere-asr/cohere-transcribe-03-2026",
        "pyannote_model": "pyannote/speaker-diarization-community-1",
    },
    "whisper": {
        "model_size": "large-v3",
        "compute_type": "float16",
        "cpu_threads": 4,
        "chunk_length_s": 30,
        "beam_size": 5,
        "best_of": 5,
        "vad_filter": True,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.2,
        "compression_ratio_threshold": 2.0,
        "log_prob_threshold": -1.0,
        "hallucination_silence_threshold": 3.0,
        "repetition_penalty": 1.0,
        "no_repeat_ngram_size": 0,
        "suppress_numerals": False,
        "hotwords": None,
        "initial_prompt": None,
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
        "forced_alignment": {
            "enabled": False,
            "backend": "torchaudio_ctc",
            "bundle_name": "VOXPOPULI_ASR_BASE_10K_FR",
            "max_segment_s": 30.0,
        },
    },
    "workflow": {
        "enable_quick_summary": True,
        "enable_speaker_detection": True,
        "enable_quality_mode": True,
        "enable_external_srt_editor_link": True,
        "enable_vad": True,
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
            "scene_affects_quality_score": False,
            "max_scene_music_ratio": 0.15,
            "max_scene_noise_ratio": 0.20,
            "max_scene_no_energy_ratio": 0.30,
            "min_scene_speech_ratio": 0.55,
            "max_scene_problem_segments": 3,
        },
        "quality_transcription": {
            "force_stt_backend": "whisper",
            "enabled_for_modes": ["quality"],
            "force_on_degraded_summary": True,
            "degraded_summary_levels": ["degrade"],
        },
        "vad": {
            "enabled_summary": True,
            "enabled_final": False,
            "adaptive": True,
            "threshold": 0.5,
            "threshold_low_quality": 0.35,
            "threshold_high_noise": 0.6,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 400,
            "min_silence_duration_ms_low_quality": 250,
            "speech_pad_ms": 200,
            "speech_pad_ms_low_quality": 350,
        },
        "audio_scene_filter": {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "target_labels": ["music", "noise"],
            "min_segment_s": 2.0,
            "min_total_muted_s": 2.0,
            "edge_keep_s": 0.15,
            "max_intervals": 100,
            "timeout_s": 300,
        },
        "audio_normalization": {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "loudnorm_enabled": True,
            "target_i": -23.0,
            "true_peak": -2.0,
            "lra": 11.0,
            "highpass_hz": None,
            "timeout_s": 300,
        },
        "speaker_realignment": {
            "enabled": True,
            "min_word_overlap_s": 0.01,
            "punctuation_chars": ".,;:!?)]}»",
        },
        "summary_llm": {
            "enabled": True,
            "model_id": "",
            "api_base": "http://127.0.0.1:8080/v1",
            "timeout_seconds": 120,
        },
        "arbitration_llm": {
            "enabled": False,
            "model_id": "",
            "api_base": "http://127.0.0.1:8080/v1",
            "timeout_seconds": 600,
            "opencode_bin": "opencode",
        },
    },
    "quality": {
        "asr_noise_markers": [
            "thank you",
            "thanks",
            "gracias",
            "obrigado",
            "e aí",
            "come on",
            "absolutely",
            "hollywood",
        ],
    },
    "diarization": {
        "cache_enabled": True,
        "cache_audio_fingerprint": True,
        "embedding_cache_enabled": True,
        "embedding_clip_seconds": 12.0,
    },
    "security": {
        "retention_days": 365,
        "allow_job_delete": True,
        "allowed_upload_extensions": [".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"],
    },
}

_CONFIG_PATH_ENV = "TRANSCRIA_CONFIG"
_DEFAULT_CONFIG_PATH = "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_default_config() -> dict:
    """Retourne une copie isolée de la configuration par défaut."""
    return copy.deepcopy(_DEFAULT_CONFIG)


def _normalize_config(cfg: dict) -> dict:
    normalized = copy.deepcopy(cfg)
    normalized.setdefault("auth", {})["enabled"] = True
    services = normalized.setdefault("services", {})
    if "arbitrage_llm_port" not in services and "qwen_port" in services:
        services["arbitrage_llm_port"] = services["qwen_port"]
    if "llm_cleanup_ports" not in services and "vllm_port" in services:
        services["llm_cleanup_ports"] = [services["vllm_port"]]
    workflow = normalized.setdefault("workflow", {})
    vad = workflow.setdefault("vad", {})
    if "enable_vad" in workflow:
        vad.setdefault("enabled_summary", bool(workflow["enable_vad"]))
        vad.setdefault("enabled_final", bool(workflow["enable_vad"]))
    return normalized


def _normalize_legacy_user_config(user_cfg: dict) -> dict:
    normalized = copy.deepcopy(user_cfg)
    services = normalized.get("services", {})
    if (
        isinstance(services, dict)
        and "vllm_port" in services
        and "llm_cleanup_ports" not in services
    ):
        services["llm_cleanup_ports"] = [services["vllm_port"]]
    workflow = normalized.get("workflow", {})
    if (
        isinstance(workflow, dict)
        and "enable_vad" in workflow
        and "vad" not in workflow
    ):
        enabled = bool(workflow["enable_vad"])
        workflow["vad"] = {
            "enabled_summary": enabled,
            "enabled_final": enabled,
        }
    return normalized


def load_config(config_path: str | None = None) -> dict:
    cfg = copy.deepcopy(_DEFAULT_CONFIG)

    if config_path is None:
        config_path = os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)

    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh)
        if user_cfg:
            user_cfg = _normalize_legacy_user_config(user_cfg)
            cfg = _deep_merge(cfg, user_cfg)

    return _normalize_config(cfg)


def get_config_path(config_path: str | None = None) -> str:
    return config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)


def save_config(cfg: dict, config_path: str | None = None) -> str:
    path = get_config_path(config_path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(_normalize_config(cfg), fh, allow_unicode=True, sort_keys=False)
    return path


_config_singleton: dict | None = None


def get_config() -> dict:
    global _config_singleton
    if _config_singleton is None:
        _config_singleton = load_config()
    return _config_singleton


def set_config(cfg: dict) -> None:
    global _config_singleton
    _config_singleton = cfg

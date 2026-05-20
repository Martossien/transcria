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
    "workflow": {
        "enable_quick_summary": True,
        "enable_speaker_detection": True,
        "enable_quality_mode": True,
        "enable_external_srt_editor_link": True,
        "enable_vad": True,
        "vad": {
            "enabled_summary": True,
            "enabled_final": False,
            "threshold": 0.5,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 400,
            "speech_pad_ms": 200,
        },
        "summary_llm": {
            "enabled": True,
            "model_id": "local/qwen3-35b",
            "api_base": "http://127.0.0.1:8080/v1",
            "timeout_seconds": 120,
        },
        "arbitration_llm": {
            "enabled": False,
            "model_id": "local/qwen3-35b-arbitrage",
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

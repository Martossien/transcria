import logging
from typing import Any

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_config, get_config_path, load_config, save_config, set_config
from transcria.config.system_detector import SystemDetector

logger = logging.getLogger(__name__)


class ConfigService:

    @staticmethod
    def load(config_path: str | None = None) -> dict:
        return load_config(config_path)

    @staticmethod
    def save(config: dict, config_path: str | None = None) -> str:
        return save_config(config, config_path)

    @staticmethod
    def get_singleton() -> dict:
        return get_config()

    @staticmethod
    def set_singleton(config: dict) -> None:
        set_config(config)

    @staticmethod
    def get_path(config_path: str | None = None) -> str:
        return get_config_path(config_path)

    @staticmethod
    def validate(config: dict):
        return validate_config(config)

    @staticmethod
    def detect_system() -> dict[str, Any]:
        return SystemDetector.detect().to_dict()

    @staticmethod
    def load_validated(config_path: str | None = None) -> tuple[dict, list[str], list[str]]:
        cfg = ConfigService.load(config_path)
        result = ConfigService.validate(cfg)
        return cfg, result.errors, result.warnings

    @staticmethod
    def save_if_valid(
        config: dict, config_path: str | None = None
    ) -> tuple[bool, list[str], list[str]]:
        result = ConfigService.validate(config)
        if not result.is_valid:
            return False, result.errors, result.warnings
        ConfigService.save(config, config_path)
        effective = ConfigService.load(config_path)
        ConfigService.set_singleton(effective)
        logger.info("Configuration sauvegardée: %s", ConfigService.get_path(config_path))
        return True, [], result.warnings

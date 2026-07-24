"""Couture 3 (temps réel) — models.live_stt_backend (défaut/validation/résolution)."""
from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config
from transcria.stt.transcriber_factory import live_backend, summary_backend


def test_defaut_null():
    assert get_default_config()["models"]["live_stt_backend"] is None


def test_factory_pas_de_repli_sur_le_principal():
    # summary retombe sur le backend principal ; le live NON (None si non configuré).
    cfg = {"models": {"stt_backend": "cohere"}}
    assert summary_backend(cfg) == "cohere"
    assert live_backend(cfg) is None


def test_factory_rend_la_valeur_configuree():
    cfg = {"models": {"stt_backend": "cohere", "live_stt_backend": "nemotron"}}
    assert live_backend(cfg) == "nemotron"


def _errors_live(cfg) -> list:
    return [e for e in validate_config(cfg).errors if "live_stt_backend" in e]


def test_validation_null_et_natif_ok():
    cfg = get_default_config()
    assert _errors_live(cfg) == []  # défaut null
    cfg["models"]["live_stt_backend"] = "whisper"
    assert _errors_live(cfg) == []


def test_validation_servi_route_ok():
    cfg = get_default_config()
    cfg["models"]["live_stt_backend"] = "voxtralrt"
    cfg.setdefault("inference", {}).setdefault("stt", {})["backends"] = {
        "voxtralrt": {"url": "http://127.0.0.1:8024/v1"}
    }
    assert _errors_live(cfg) == []


def test_validation_backend_inexistant_erreur():
    cfg = get_default_config()
    cfg["models"]["live_stt_backend"] = "inexistant"
    assert _errors_live(cfg)  # au moins une erreur mentionnant live_stt_backend

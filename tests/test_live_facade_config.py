"""Phase K — section `live.facade` : défaut OFF + validation (opt-in)."""
from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config


def _live_errors(cfg) -> list:
    return [e for e in validate_config(cfg).errors if e.startswith("live")]


def test_defaut_facade_desactivee():
    assert get_default_config()["live"]["facade"]["enabled"] is False


def test_defaut_valide():
    assert _live_errors(get_default_config()) == []


def test_enabled_non_booleen_erreur():
    cfg = get_default_config()
    cfg["live"]["facade"]["enabled"] = "oui"
    assert _live_errors(cfg)


def test_facade_mauvais_type_erreur():
    cfg = get_default_config()
    cfg["live"]["facade"] = "x"
    assert _live_errors(cfg)


def test_live_mauvais_type_erreur():
    cfg = get_default_config()
    cfg["live"] = "x"
    assert _live_errors(cfg)


def test_section_live_absente_toleree():
    cfg = get_default_config()
    cfg.pop("live", None)
    assert _live_errors(cfg) == []

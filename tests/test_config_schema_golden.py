"""GOLDEN du schéma de config — filet posé AVANT la découpe de config_schema.py (vague 0, B0).

Trois empreintes figées depuis l'implémentation d'AVANT découpe (tests/golden_config_schema.json) :

- ``config_vide`` : les 36 messages exacts sur ``{}`` — perdre un appel de section dans le
  dispatcher ferait disparaître des messages ; les tests unitaires par section, eux, ne le
  verraient pas ;
- ``defaults_loader`` : la config par défaut COMPLÈTE du loader valide sans AUCUNE erreur —
  l'invariant qui a déjà attrapé des clés fantômes ;
- ``multi_invalide_depuis_defauts`` : une faute par futur domaine (platform, auth, stt,
  orchestration, audio, live) → les 9 messages exacts, mot pour mot. Les MESSAGES font
  partie du comportement : l'admin les lit, la doc les cite.

Toute évolution VOULUE du schéma (nouveau check, nouveau message) met à jour le JSON
explicitement — c'est le prix d'un golden.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config

GOLDEN = json.loads((Path(__file__).parent / "golden_config_schema.json").read_text(encoding="utf-8"))


def test_empty_config_errors_are_the_golden():
    assert sorted(validate_config({}).errors) == GOLDEN["config_vide"]


def test_loader_defaults_validate_clean():
    errors = validate_config(copy.deepcopy(get_default_config())).errors
    assert sorted(errors) == GOLDEN["defaults_loader"] == []


def test_one_fault_per_domain_yields_the_golden_messages():
    bad = copy.deepcopy(get_default_config())
    bad["server"]["port"] = "pas-un-port"
    bad["auth"]["backend"] = "kerberos"
    bad["models"]["stt_backend"] = "inexistant"
    bad["workflow"]["multi_stt"] = {"enabled": "oui"}
    bad["diarization"]["pipeline_params"] = 12
    bad["live"]["facade"] = {"enabled": "oui"}
    assert sorted(validate_config(bad).errors) == GOLDEN["multi_invalide_depuis_defauts"]

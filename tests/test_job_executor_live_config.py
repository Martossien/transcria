"""Le worker suit la configuration VIVANTE : une sauvegarde depuis Réglages (set_config remplace le
singleton) doit valoir pour le job suivant — vécu 2026-11-07 : STT passée à kroko, résumé lancé
avec cohere parce que l'exécuteur gardait l'objet reçu au démarrage."""
from __future__ import annotations

import copy

from transcria.config import get_config, set_config
from transcria.services.job_executor import JobExecutorService


def test_executor_suit_le_singleton_quand_il_l_a_recu(app):
    before = get_config()
    svc = JobExecutorService(app, before, run_scheduler=False)
    assert svc.config is before
    after = copy.deepcopy(before)
    after.setdefault("models", {})["stt_backend"] = "kroko"
    set_config(after)
    try:
        assert svc.config is after and svc.config["models"]["stt_backend"] == "kroko"
    finally:
        set_config(before)
    assert svc.config is before


def test_executor_garde_une_config_injectee(app):
    injected = {"models": {"stt_backend": "whisper"}, "workflow": {}}
    svc = JobExecutorService(app, injected, run_scheduler=False)
    before = get_config()
    set_config(copy.deepcopy(before))
    try:
        assert svc.config is injected                       # tests/outils : jamais remplacée par le singleton
    finally:
        set_config(before)
    svc.config = {"models": {}}
    assert svc.config == {"models": {}}

"""Façade STT et modes de déploiement (all-in-one vs split web/scheduler)."""
from __future__ import annotations

import logging

from transcria.app_services import warn_facade_inference_on_web_role


class _Recorder(logging.Logger):
    def __init__(self):
        super().__init__("test")
        self.warnings: list[str] = []

    def warning(self, msg, *args, **kwargs):      # noqa: D102
        self.warnings.append(msg % args if args else msg)


def _run(enabled: bool, role: str) -> list[str]:
    log = _Recorder()
    warn_facade_inference_on_web_role({"live": {"facade": {"enabled": enabled}}}, role, log)
    return log.warnings


def test_avertit_si_facade_active_sur_role_web():
    """SPLIT : la façade transcrit DANS le process web — une frontale sans GPU échouerait
    silencieusement. L'opérateur doit être prévenu au démarrage, pas en pleine réunion."""
    warnings = _run(True, "web")
    assert len(warnings) == 1
    assert "web" in warnings[0] and "/v1/audio/transcriptions" in warnings[0]


def test_silencieux_en_all_in_one():
    assert _run(True, "all") == []                # le nœud porte aussi la file : cas normal


def test_silencieux_si_facade_desactivee():
    assert _run(False, "web") == []               # rien à signaler : pas d'inférence ici


def test_silencieux_sur_role_scheduler():
    assert _run(True, "scheduler") == []          # le scheduler a le matériel par construction

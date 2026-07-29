"""Bot — FSM de cycle de vie (BotSession) avec driver navigateur factice."""
from __future__ import annotations

import asyncio

from connector_service.bot.orchestrator import BotSession, BotState


class FakeDriver:
    def __init__(self, *, admitted=True, end_reason="left_alone"):
        self._admitted = admitted
        self._end_reason = end_reason
        self.calls: list = []
        self.states: list = []

    async def open(self, url):
        self.calls.append(("open", url))

    async def request_join(self, name):
        self.calls.append(("request_join", name))

    async def wait_admission(self, timeout_s):
        self.calls.append(("wait_admission", timeout_s))
        return self._admitted

    async def run_until_ended(self):
        self.calls.append(("run_until_ended",))
        return self._end_reason

    async def leave(self):
        self.calls.append(("leave",))

    async def close(self):
        self.calls.append(("close",))


def test_cycle_admis_complet():
    driver = FakeDriver(admitted=True, end_reason="left_alone")
    session = BotSession(driver, display_name="TranscrIA-bot", admission_timeout_s=30)
    outcome = asyncio.run(session.run("https://meet.jit.si/salle"))
    assert outcome.admitted is True and outcome.reason == "left_alone"
    assert session.state == BotState.DONE
    names = [c[0] for c in driver.calls]
    assert names == ["open", "request_join", "wait_admission", "run_until_ended",
                     "leave", "close"]
    assert ("wait_admission", 30) in driver.calls


def test_non_admis_ne_capture_pas_et_ferme():
    driver = FakeDriver(admitted=False)
    outcome = asyncio.run(BotSession(driver).run("https://meet.jit.si/salle"))
    assert outcome.admitted is False and outcome.reason == "admission_timeout"
    names = [c[0] for c in driver.calls]
    assert "run_until_ended" not in names and "leave" not in names   # pas de capture
    assert names[-1] == "close"                                      # nettoyage garanti


def test_non_admis_remonte_la_cause_fine_du_driver():
    """Un driver qui consigne `admission_reason` (ex. Jitsi : password_required) la voit
    remontée dans `outcome.detail` — sans changer `reason` ni les codes de sortie."""
    driver = FakeDriver(admitted=False)
    driver.admission_reason = "password_required"
    outcome = asyncio.run(BotSession(driver).run("https://meet.jit.si/salle"))
    assert outcome.reason == "admission_timeout"      # le contrat de l'orchestrateur ne bouge pas
    assert outcome.detail == "password_required"


def test_non_admis_sans_introspection_driver_detail_vide():
    outcome = asyncio.run(BotSession(FakeDriver(admitted=False)).run("https://meet.jit.si/x"))
    assert outcome.detail == ""


def test_erreur_driver_devient_outcome_error_et_ferme():
    class BoomDriver(FakeDriver):
        async def run_until_ended(self):
            raise RuntimeError("navigateur mort")

    driver = BoomDriver(admitted=True)
    outcome = asyncio.run(BotSession(driver).run("https://x"))       # ne crashe PAS l'appelant
    assert outcome.reason == "error" and outcome.admitted is True    # admis puis erreur
    assert ("close",) in driver.calls                                # nettoyage garanti


def test_on_state_notifie_chaque_transition():
    """Vague 4 : le runner relaie les états au portail via ce rappel — jamais par les logs."""
    driver = FakeDriver(admitted=True, end_reason="left_alone")
    seen: list[str] = []
    session = BotSession(driver, on_state=lambda s: seen.append(s.value))
    asyncio.run(session.run("https://meet.jit.si/x"))
    assert seen[:3] == ["waiting_admission", "active", "leaving"]


def test_observateur_defaillant_ne_casse_pas_le_cycle():
    def boom(_):
        raise RuntimeError("observateur cassé")
    outcome = asyncio.run(BotSession(FakeDriver(admitted=True), on_state=boom)
                          .run("https://meet.jit.si/x"))
    assert outcome.admitted is True

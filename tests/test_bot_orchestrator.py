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


def test_close_appele_meme_sur_erreur():
    class BoomDriver(FakeDriver):
        async def run_until_ended(self):
            raise RuntimeError("navigateur mort")

    driver = BoomDriver(admitted=True)
    try:
        asyncio.run(BotSession(driver).run("https://x"))
    except RuntimeError:
        pass
    assert ("close",) in driver.calls                                # finally → close

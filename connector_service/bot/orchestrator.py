"""Cycle de vie du bot de réunion — FSM `join → admission → active → leave`, testable.

Le pilotage navigateur réel (Playwright) est derrière le Protocol `BrowserDriver` INJECTÉ →
la machine à états est **testable en CI** avec un driver factice ; l'implémentation réelle
(par plateforme) est confirmée au gate. S'inspire des cycles de vie éprouvés (1 conteneur/
réunion, fins par silence prolongé / éjection / durée max) — sans copier de code tiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class BotState(str, Enum):
    JOINING = "joining"
    WAITING_ADMISSION = "waiting_admission"
    ACTIVE = "active"
    LEAVING = "leaving"
    DONE = "done"


class BrowserDriver(Protocol):
    """Pilotage navigateur d'une plateforme. Chaque méthode isole une étape du cycle de vie ;
    la capture audio (payload JS → pont PCM) démarre côté page dès `open` et coule pendant
    `run_until_ended`."""

    async def open(self, meeting_url: str) -> None: ...
    async def request_join(self, display_name: str) -> None: ...
    async def wait_admission(self, timeout_s: float) -> bool: ...   # True=admis, False=refus/timeout
    async def run_until_ended(self) -> str: ...                     # motif : left_alone/removed/…
    async def leave(self) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class BotOutcome:
    admitted: bool
    reason: str            # admission_timeout / left_alone / removed / max_duration / stopped / error


class BotSession:
    """Déroule le cycle de vie d'un bot pour UNE réunion. `driver` = `BrowserDriver` injecté.
    `close()` est toujours appelé (même sur échec) — un conteneur/réunion, nettoyage garanti."""

    def __init__(self, driver: BrowserDriver, *, display_name: str = "TranscrIA",
                 admission_timeout_s: float = 120.0) -> None:
        self._driver = driver
        self._name = display_name
        self._admission_timeout_s = admission_timeout_s
        self.state = BotState.JOINING

    async def run(self, meeting_url: str) -> BotOutcome:
        try:
            await self._driver.open(meeting_url)
            self.state = BotState.WAITING_ADMISSION
            await self._driver.request_join(self._name)
            admitted = await self._driver.wait_admission(self._admission_timeout_s)
            if not admitted:
                return BotOutcome(admitted=False, reason="admission_timeout")

            self.state = BotState.ACTIVE
            reason = await self._driver.run_until_ended()

            self.state = BotState.LEAVING
            await self._driver.leave()
            return BotOutcome(admitted=True, reason=reason)
        finally:
            self.state = BotState.DONE
            await self._driver.close()

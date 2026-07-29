"""Cycle de vie du bot de réunion — FSM `join → admission → active → leave`, testable.

Le pilotage navigateur réel (Playwright) est derrière le Protocol `BrowserDriver` INJECTÉ →
la machine à états est **testable en CI** avec un driver factice ; l'implémentation réelle
(par plateforme) est confirmée au gate. S'inspire des cycles de vie éprouvés (1 conteneur/
réunion, fins par silence prolongé / éjection / durée max) — sans copier de code tiers.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

_log = logging.getLogger(__name__)


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
    # Cause FINE d'un refus d'admission quand le driver la connaît (ex. Jitsi :
    # password_required / auth_required / lobby_waiting / timeout). Diagnostic seulement —
    # `reason` et les codes de sortie restent le contrat de l'orchestrateur.
    detail: str = ""


class BotSession:
    """Déroule le cycle de vie d'un bot pour UNE réunion. `driver` = `BrowserDriver` injecté.
    `close()` est toujours appelé (même sur échec) — un conteneur/réunion, nettoyage garanti."""

    def __init__(self, driver: BrowserDriver, *, display_name: str = "TranscrIA",
                 admission_timeout_s: float = 120.0, on_state=None) -> None:
        self._driver = driver
        self._name = display_name
        self._admission_timeout_s = admission_timeout_s
        # Rappel optionnel à CHAQUE transition (vague 4) : le runner relaie l'état au portail
        # (salle d'attente, en réunion…) sans parser les logs. Best-effort : ne casse jamais.
        self._on_state = on_state
        self._state = BotState.JOINING

    @property
    def state(self) -> BotState:
        return self._state

    @state.setter
    def state(self, value: BotState) -> None:
        self._state = value
        if self._on_state is not None:
            try:
                self._on_state(value)
            except Exception:  # noqa: BLE001 — un observateur ne casse jamais le cycle de vie
                _log.debug("observateur d'état en échec", exc_info=True)

    async def run(self, meeting_url: str) -> BotOutcome:
        admitted_flag = False
        try:
            await self._driver.open(meeting_url)
            self.state = BotState.WAITING_ADMISSION
            await self._driver.request_join(self._name)
            admitted = await self._driver.wait_admission(self._admission_timeout_s)
            if not admitted:
                # Le driver peut consigner POURQUOI (mot de passe, auth, timeout…) — précieuse
                # pour l'exploitant, mais optionnelle : tout driver n'a pas cette introspection.
                detail = str(getattr(self._driver, "admission_reason", "") or "")
                return BotOutcome(admitted=False, reason="admission_timeout", detail=detail)

            admitted_flag = True
            self.state = BotState.ACTIVE
            reason = await self._driver.run_until_ended()

            self.state = BotState.LEAVING
            await self._driver.leave()
            return BotOutcome(admitted=True, reason=reason)
        except Exception:                            # le cycle de vie ne crashe pas l'appelant
            _log.exception("échec du cycle de vie du bot (%s)", meeting_url)
            with contextlib.suppress(Exception):
                await self._driver.leave()
            return BotOutcome(admitted=admitted_flag, reason="error")
        finally:
            self.state = BotState.DONE
            await self._driver.close()

"""Squelette du service connecteur async (A0 — DoD « service démarrable/arrêtable »).

Process ISOLÉ (aucun import de transcria) : il découvre des occurrences de réunion et
les réconcilie via `ProviderReconciler` → pont HTTP. `run_once()` est l'unité testable ;
`run_forever()` boucle en respectant `stop()`.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.reconciler import ProviderReconciler, ReconcileOutcome


class ConnectorService:
    def __init__(
        self,
        reconciler: ProviderReconciler,
        discover_occurrences: Callable[[], Awaitable[list[ExternalMeetingOccurrence]]],
        *,
        interval_s: float = 60.0,
    ) -> None:
        self._reconciler = reconciler
        self._discover = discover_occurrences
        self._interval_s = interval_s
        self._running = False
        # Ensemble local des clés déjà importées (optimisation ; le garde ultime reste
        # l'idempotence serveur). Persisté par un vrai déploiement ; en mémoire ici.
        self._seen: set[str] = set()

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def run_once(self) -> list[ReconcileOutcome]:
        """Un cycle de réconciliation sur toutes les occurrences découvertes."""
        outcomes: list[ReconcileOutcome] = []
        for occurrence in await self._discover():
            outcomes.extend(
                await self._reconciler.reconcile(occurrence, already_imported=self._seen)
            )
        return outcomes

    async def run_forever(self) -> None:
        """Boucle jusqu'à `stop()`. Un cycle qui échoue ne tue pas la boucle."""
        await self.start()
        while self._running:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — un cycle raté ne doit pas arrêter le service
                pass
            await self._interruptible_sleep(self._interval_s)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Attend `seconds` en tranches courtes, réactif à `stop()`. Rend TOUJOURS la main
        à l'event loop au moins une fois (même `seconds=0`) — sinon boucle occupée."""
        await asyncio.sleep(0)
        remaining = seconds
        while self._running and remaining > 0:
            slice_s = min(0.1, remaining)
            await asyncio.sleep(slice_s)
            remaining -= slice_s

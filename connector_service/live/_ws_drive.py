"""Boucle commune envoi(pump)/réception des transports STT WebSocket (Kyutai, WhisperLiveKit).

Une task `pump` (envoi de l'audio) tourne concurremment à la boucle `recv` (événements
serveur décodés). Robustesse (bugs corrigés) :
- si le pump MEURT (échec `send`, erreur du provider), on FERME le socket pour débloquer
  `recv` — sinon deadlock : le serveur n'a plus d'audio et n'émet plus rien ;
- l'exception du pump est RÉCUPÉRÉE et re-propagée au lieu d'un « Task exception was never
  retrieved » silencieux (la cause racine remonte au lieu d'une fin de flux muette).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any


async def drive_recv(ws: Any, pump: Coroutine[Any, Any, None],
                     decode: Callable[[Any], dict]) -> AsyncIterator[dict]:
    """Yield les événements décodés du socket pendant que `pump` pousse l'audio."""
    pump_task = asyncio.ensure_future(pump)

    def _on_pump_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            asyncio.ensure_future(ws.close())          # pump mort → débloque recv
    pump_task.add_done_callback(_on_pump_done)

    try:
        while True:
            try:
                raw = await ws.recv()
            except StopAsyncIteration:
                break
            yield decode(raw)
        if pump_task.done() and not pump_task.cancelled():
            exc = pump_task.exception()
            if exc is not None:
                raise exc                               # surface la cause racine si le pump a échoué
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task                             # récupère l'exception / supprime le warning
        await ws.close()

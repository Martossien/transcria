"""Suivi de charge léger pour moteurs GPU sérialisés.

Les moteurs in-process (`voice-embed`, `diarize`) restent volontairement mono-GPU :
un seul calcul à la fois. Ce tracker encapsule le verrou existant et expose un
snapshot stable pour `/capabilities`, sans changer la politique d'exécution.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


class SerializedLoadTracker:
    """Verrou mono-capacité avec compteurs observables.

    Args:
        name: nom métier du moteur, utilisé dans les logs et le snapshot.
        logger: logger applicatif du moteur.
        clock: horloge injectable pour les tests.
    """

    def __init__(
        self,
        name: str,
        logger: logging.Logger,
        *,
        clock=None,
    ) -> None:
        self.name = name
        self._logger = logger
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._inflight = 0
        self._queued = 0
        self._last_wait_s = 0.0

    @contextmanager
    def acquire(self, operation: str) -> Iterator[None]:
        """Acquiert le verrou du moteur et trace l'attente éventuelle."""
        start = self._clock()
        acquired_immediately = self._lock.acquire(blocking=False)
        if not acquired_immediately:
            with self._state_lock:
                self._queued += 1
                queued = self._queued
            self._logger.info(
                "%s occupé — attente du verrou moteur | operation=%s queued=%d",
                self.name,
                operation,
                queued,
            )
            self._lock.acquire()
            wait_s = max(0.0, self._clock() - start)
            with self._state_lock:
                self._queued = max(0, self._queued - 1)
                self._last_wait_s = wait_s
                self._inflight = 1
            self._logger.info(
                "%s verrou moteur acquis après attente | operation=%s wait=%.3fs queued=%d",
                self.name,
                operation,
                wait_s,
                self.snapshot()["queued"],
            )
        else:
            with self._state_lock:
                self._inflight = 1
                self._last_wait_s = 0.0

        try:
            yield
        finally:
            with self._state_lock:
                self._inflight = 0
            self._lock.release()

    def snapshot(self) -> dict:
        """État de charge publié dans `/capabilities`."""
        with self._state_lock:
            inflight = self._inflight
            queued = self._queued
            last_wait_s = self._last_wait_s
        return {
            "capacity": 1,
            "inflight": inflight,
            "queued": queued,
            "busy": inflight > 0,
            "last_wait_s": round(last_wait_s, 3),
        }

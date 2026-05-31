"""Réveil instantané de l'ordonnanceur via PostgreSQL ``LISTEN/NOTIFY`` (Phase B / B9, D2).

Sans cela, un worker web (rôle ``web``, process distinct de l'ordonnanceur) ne peut pas
réveiller le thread de dispatch : il faut attendre le prochain *poll* (``poll_interval_s``,
défaut 5 s). Avec ``LISTEN/NOTIFY``, l'enqueue émet un ``NOTIFY`` (cf.
`QueueStore.notify_queue`) et ce listener — une **connexion psycopg dédiée** sur un thread
de fond — réveille l'ordonnanceur immédiatement.

Le polling reste le **filet de sûreté** : si une notification est manquée (listener en
reconnexion, redémarrage), l'itération périodique rattrape. C'est purement une optimisation
de latence, jamais une condition de correction.

Sur un dialecte sans ``LISTEN/NOTIFY`` (SQLite, dev/tests mono-process), `for_engine`
renvoie ``None`` : le réveil intra-process (`threading.Event`) suffit.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Canal partagé par toutes les instances TranscrIA d'une même base.
QUEUE_NOTIFY_CHANNEL = "transcria_queue"

_MAX_RECONNECT_BACKOFF_S = 30.0


def engine_conninfo(engine: Engine) -> str:
    """Chaîne libpq pour psycopg depuis l'URL SQLAlchemy (``postgresql+psycopg`` → ``postgresql``)."""
    rendered = engine.url.render_as_string(hide_password=False)
    return rendered.replace("postgresql+psycopg://", "postgresql://", 1)


class QueueNotifyListener:
    """Écoute ``NOTIFY transcria_queue`` sur une connexion dédiée et déclenche un rappel.

    Robuste aux coupures : sur erreur, ferme la connexion, attend (backoff exponentiel
    borné) puis se reconnecte. Le polling de l'ordonnanceur garantit la correction pendant
    ces fenêtres. `stop()` est idempotent et interrompt l'attente bloquante en fermant la
    connexion.
    """

    def __init__(
        self,
        connect_fn: Callable[[], Any],
        on_notify: Callable[[], None],
        *,
        timeout_s: float = 5.0,
        channel: str = QUEUE_NOTIFY_CHANNEL,
    ) -> None:
        self._connect_fn = connect_fn
        self._on_notify = on_notify
        self._timeout_s = max(0.5, float(timeout_s))
        self._channel = channel
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._conn_lock = threading.Lock()

    @classmethod
    def for_engine(
        cls,
        engine: Engine,
        on_notify: Callable[[], None],
        *,
        timeout_s: float = 5.0,
    ) -> QueueNotifyListener | None:
        """Construit un listener pour un moteur **PostgreSQL**, sinon ``None`` (no-op)."""
        if engine.dialect.name != "postgresql":
            return None
        import psycopg

        conninfo = engine_conninfo(engine)
        return cls(
            lambda: psycopg.connect(conninfo, autocommit=True),
            on_notify,
            timeout_s=timeout_s,
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="transcria-queue-notify", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._close_conn()  # interrompt un notifies() bloquant
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Attend que ``LISTEN`` soit établi (utile pour éviter une course au démarrage)."""
        return self._ready.wait(timeout)

    def _close_conn(self) -> None:
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — fermeture best-effort
                pass

    def _connect_and_listen(self) -> None:
        conn = self._connect_fn()
        conn.execute(f"LISTEN {self._channel}")
        with self._conn_lock:
            self._conn = conn
        self._ready.set()
        logger.info("Écoute des notifications de file sur le canal '%s'", self._channel)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self._conn is None:
                    self._connect_and_listen()
                    backoff = 1.0
                for _notify in self._conn.notifies(timeout=self._timeout_s):
                    if self._stop.is_set():
                        break
                    # Un seul réveil suffit : l'ordonnanceur draine toute la file au tick.
                    self._on_notify()
                    break
            except Exception as exc:  # noqa: BLE001 — le polling assure la correction
                if self._stop.is_set():
                    break
                logger.warning(
                    "Écoute LISTEN/NOTIFY interrompue (%s) — reconnexion dans %.0fs "
                    "(le polling assure le repli)", exc, backoff,
                )
                self._ready.clear()
                self._close_conn()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _MAX_RECONNECT_BACKOFF_S)
        self._close_conn()

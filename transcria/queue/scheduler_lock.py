"""Verrou consultatif PostgreSQL garantissant un **ordonnanceur de file unique**
(Phase B / C1, invariant I1).

Un seul process doit drainer ``job_queue`` à la fois. Plutôt que de s'en remettre
au seul déploiement (un service systemd dédié), on pose un garde-fou en base :
``pg_try_advisory_lock`` sur une clé fixe, tenu pour toute la vie du scheduler via
une **connexion dédiée** (le verrou est lié à la session ; PostgreSQL le libère
automatiquement si le process meurt — pas de verrou orphelin).

Sur les dialectes sans verrou consultatif (SQLite, dev/tests mono-process), le
verrou est un no-op « toujours acquis » : l'unicité y est assurée par le fait qu'un
seul process tourne.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

# Clé arbitraire mais stable, partagée par toutes les instances TranscrIA d'une même
# base. (bigint 64 bits attendu par pg_try_advisory_lock(key).)
SCHEDULER_ADVISORY_LOCK_KEY = 0x7A5C4ED1


class SchedulerLock:
    """Verrou « ordonnanceur unique ». À acquérir au démarrage du scheduler, à
    libérer à son arrêt. Non réentrant, non thread-safe (un seul propriétaire)."""

    def __init__(self, engine: Engine, key: int = SCHEDULER_ADVISORY_LOCK_KEY):
        self._engine = engine
        self._key = int(key)
        self._conn: Connection | None = None
        self.acquired = False

    @property
    def _is_postgres(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def try_acquire(self) -> bool:
        """Tente de prendre le verrou sans bloquer. Renvoie True si ce process le
        détient désormais, False s'il est déjà tenu ailleurs."""
        if self.acquired:
            return True
        if not self._is_postgres:
            # Pas de coordination inter-process possible : on suppose le mono-process.
            self.acquired = True
            return True
        conn = self._engine.connect()
        try:
            got = bool(conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}
            ).scalar())
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            self.acquired = False
            return False
        # Connexion gardée ouverte = verrou de session maintenu.
        self._conn = conn
        self.acquired = True
        return True

    def release(self) -> None:
        """Libère le verrou (idempotent). Ne nécessite pas de contexte applicatif :
        utilise la connexion dédiée déjà ouverte."""
        if self._conn is not None:
            try:
                self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
            except Exception as exc:  # noqa: BLE001 — la fermeture libère de toute façon le verrou
                logger.warning("Libération du verrou scheduler : %s", exc)
            finally:
                self._conn.close()
                self._conn = None
        self.acquired = False

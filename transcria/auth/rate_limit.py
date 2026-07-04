"""Limitation des tentatives de connexion — chantier C3.3 (docs/archive/RELEASE_0.2.0.md).

Constat (audit sécurité) : aucune protection contre le bourrinage sur ``/login``.
Ici, un compteur EN MÉMOIRE par (IP, identifiant) avec fenêtre glissante et backoff :
au-delà du seuil, la tentative est refusée (429) pendant un court blocage, journalisé
en audit. Volontairement simple et sans dépendance (pas de Redis) — suffisant pour un
déploiement local/mono-process ; en multi-process, chaque worker a son compteur (le
blocage reste efficace car les tentatives d'une même IP se répartissent mal).

Pur et injectable (horloge ``now_fn``) → testable sans dormir.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    attempts: list[float] = field(default_factory=list)
    blocked_until: float = 0.0


class LoginRateLimiter:
    """Fenêtre glissante : ``max_attempts`` échecs en ``window_s`` → blocage
    ``block_s``. Un succès efface le compteur de la clé."""

    def __init__(self, *, max_attempts: int = 5, window_s: float = 300.0,
                 block_s: float = 300.0, now_fn=time.monotonic) -> None:
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.block_s = block_s
        self._now = now_fn
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str, username: str) -> str:
        return f"{ip}|{username.lower().strip()}"

    def is_blocked(self, ip: str, username: str) -> float:
        """Renvoie les secondes de blocage restantes (0 si non bloqué)."""
        now = self._now()
        with self._lock:
            bucket = self._buckets.get(self._key(ip, username))
            if bucket and bucket.blocked_until > now:
                return round(bucket.blocked_until - now, 1)
        return 0.0

    def record_failure(self, ip: str, username: str) -> float:
        """Enregistre un échec. Renvoie les secondes de blocage si le seuil est franchi."""
        now = self._now()
        with self._lock:
            bucket = self._buckets.setdefault(self._key(ip, username), _Bucket())
            bucket.attempts = [t for t in bucket.attempts if now - t < self.window_s]
            bucket.attempts.append(now)
            if len(bucket.attempts) >= self.max_attempts:
                bucket.blocked_until = now + self.block_s
                return self.block_s
        return 0.0

    def record_success(self, ip: str, username: str) -> None:
        with self._lock:
            self._buckets.pop(self._key(ip, username), None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Instance partagée par l'application (un process = un compteur).
login_rate_limiter = LoginRateLimiter()

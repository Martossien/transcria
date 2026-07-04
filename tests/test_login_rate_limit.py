"""Tests C3.3 — limitation des tentatives de connexion (docs/archive/RELEASE_0.2.0.md)."""
from __future__ import annotations

from transcria.auth.rate_limit import LoginRateLimiter


class _Clock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += s


class TestRateLimiter:
    def test_bloque_apres_le_seuil(self):
        clock = _Clock()
        rl = LoginRateLimiter(max_attempts=3, window_s=300, block_s=300, now_fn=clock)
        assert rl.record_failure("1.2.3.4", "admin") == 0     # 1
        assert rl.record_failure("1.2.3.4", "admin") == 0     # 2
        assert rl.record_failure("1.2.3.4", "admin") == 300   # 3 → bloqué
        assert rl.is_blocked("1.2.3.4", "admin") > 0

    def test_deblocage_apres_expiration(self):
        clock = _Clock()
        rl = LoginRateLimiter(max_attempts=2, window_s=300, block_s=120, now_fn=clock)
        rl.record_failure("ip", "u")
        rl.record_failure("ip", "u")
        assert rl.is_blocked("ip", "u") > 0
        clock.advance(121)
        assert rl.is_blocked("ip", "u") == 0

    def test_fenetre_glissante_oublie_les_vieux_echecs(self):
        clock = _Clock()
        rl = LoginRateLimiter(max_attempts=3, window_s=100, block_s=300, now_fn=clock)
        rl.record_failure("ip", "u")
        rl.record_failure("ip", "u")
        clock.advance(101)                       # les 2 premiers sortent de la fenêtre
        assert rl.record_failure("ip", "u") == 0  # compte comme le 1er, pas le 3e

    def test_succes_efface_le_compteur(self):
        clock = _Clock()
        rl = LoginRateLimiter(max_attempts=2, window_s=300, block_s=300, now_fn=clock)
        rl.record_failure("ip", "u")
        rl.record_success("ip", "u")
        assert rl.record_failure("ip", "u") == 0  # reparti de zéro

    def test_isolation_par_ip_et_compte(self):
        clock = _Clock()
        rl = LoginRateLimiter(max_attempts=2, window_s=300, block_s=300, now_fn=clock)
        rl.record_failure("ip1", "admin")
        rl.record_failure("ip1", "admin")
        assert rl.is_blocked("ip1", "admin") > 0
        assert rl.is_blocked("ip2", "admin") == 0   # autre IP non affectée
        assert rl.is_blocked("ip1", "autre") == 0   # autre compte non affecté


class TestLoginIntegration:
    def test_login_bloque_apres_5_echecs(self, client):
        for _ in range(5):
            client.post("/login", data={"username": "admin", "password": "faux"})
        r = client.post("/login", data={"username": "admin", "password": "faux"})
        assert r.status_code == 429
        from transcria.auth.rate_limit import login_rate_limiter
        login_rate_limiter.reset()   # ne pas polluer les autres tests

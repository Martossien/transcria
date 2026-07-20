"""Chantier identité lot 3 — proxy de confiance (Remote-User/Remote-Groups).

Toute la sécurité du connecteur tient dans la garde d'adresse SOCKET : ces
tests déroulent le flux réel à travers les routes (auto-login, usurpation
depuis une IP non déclarée, X-Forwarded-For ignoré, refus de mapping,
break-glass, logout sans reconnexion immédiate).
"""
from __future__ import annotations

import os

import pytest

from transcria.config import _deep_merge, load_config

_MAPPING = {"claim": "groups",
            "rules": [{"group": "transcria-admins", "role": "admin"},
                      {"group": "transcria-operateurs", "role": "operator"}],
            "default": "deny"}

_HEADERS_BOSS = {"Remote-User": "proxy-boss",
                 "Remote-Groups": "autre, transcria-admins",
                 "Remote-Name": "Boss Proxy",
                 "Remote-Email": "proxy-boss@corp.example"}


def _make_app(_pg_database, **auth_overrides):
    os.environ["TRANSCRIA_DATABASE_URL"] = _pg_database
    cfg = load_config()
    cfg = _deep_merge(cfg, {
        "auth": {"backend": "proxy",
                 # Même graine admin que conftest (robustesse à l'ordre des modules).
                 "first_admin_password": "admin-change-me",
                 "proxy": {"trusted_ips": ["127.0.0.1"]},
                 "role_mapping": _MAPPING,
                 **auth_overrides},
        "storage": {"jobs_dir": "/tmp/transcria-proxy-tests-jobs"},
    })
    from app import create_app
    app = create_app(config=cfg, start_background_services=False)
    app.config.update({"TESTING": True})
    return app


@pytest.fixture(scope="module")
def proxy_app(_pg_database):
    return _make_app(_pg_database)


class TestAutoLogin:
    def test_login_complet_jit_role_et_session(self, proxy_app):
        with proxy_app.test_client() as client:
            r = client.get("/login", headers=_HEADERS_BOSS)
            assert r.status_code == 302 and "/auth/proxy/login" in r.headers["Location"]
            r2 = client.get(r.headers["Location"], headers=_HEADERS_BOSS)
            assert r2.status_code == 302 and r2.headers["Location"].endswith("/")
            with proxy_app.app_context():
                from transcria.auth.store import UserStore
                u = UserStore.get_by_external("proxy", "proxy-boss")
                assert u is not None and u.role == "admin"
                assert u.email == "proxy-boss@corp.example" and u.display_name == "Boss Proxy"
            assert client.get("/").status_code == 200   # la session vaut login

    def test_role_remplace_a_chaque_login(self, proxy_app):
        headers = {"Remote-User": "proxy-retro", "Remote-Groups": "transcria-admins"}
        with proxy_app.test_client() as client:
            client.get("/auth/proxy/login", headers=headers)
        # Retiré des admins → rétrogradé operator au login suivant (jamais max()).
        headers["Remote-Groups"] = "transcria-operateurs"
        with proxy_app.test_client() as client:
            client.get("/auth/proxy/login", headers=headers)
        with proxy_app.app_context():
            from transcria.auth.store import UserStore
            assert UserStore.get_by_external("proxy", "proxy-retro").role == "operator"


class TestGardeAdresseSocket:
    def test_usurpation_depuis_ip_non_declaree(self, proxy_app, caplog):
        with proxy_app.test_client() as client:
            r = client.get("/auth/proxy/login", headers={"Remote-User": "proxy-intrus",
                                                         "Remote-Groups": "transcria-admins"},
                           environ_base={"REMOTE_ADDR": "10.9.9.9"})
        assert r.status_code == 401
        assert "usurpation" in caplog.text           # WARNING journalisé avec l'IP
        assert "10.9.9.9" in caplog.text
        with proxy_app.app_context():
            from transcria.auth.store import UserStore
            assert UserStore.get_by_external("proxy", "proxy-intrus") is None   # rien créé

    def test_x_forwarded_for_jamais_cru(self, proxy_app):
        """XFF est falsifiable par construction : une IP non déclarée qui se
        prétend 127.0.0.1 via X-Forwarded-For reste refusée (adresse SOCKET)."""
        with proxy_app.test_client() as client:
            r = client.get("/auth/proxy/login",
                           headers={**_HEADERS_BOSS, "X-Forwarded-For": "127.0.0.1"},
                           environ_base={"REMOTE_ADDR": "10.9.9.9"})
        assert r.status_code == 401

    def test_en_tete_absent_depuis_ip_de_confiance(self, proxy_app):
        with proxy_app.test_client() as client:
            r = client.get("/auth/proxy/login")     # IP de confiance, aucun en-tête
            assert r.status_code == 401
            assert 'name="password"' in r.get_data(as_text=True)   # secours affiché


class TestRefusEtBreakGlass:
    def test_refus_sans_groupe_mappe(self, proxy_app):
        with proxy_app.test_client() as client:
            r = client.get("/auth/proxy/login", headers={"Remote-User": "proxy-sans-groupe",
                                                         "Remote-Groups": "rien"})
            assert r.status_code == 403
            body = r.get_data(as_text=True)
            assert "Accès non attribué" in body
            assert 'name="password"' in body   # sinon boucle d'auto-login sur le refus
        with proxy_app.app_context():
            from transcria.auth.store import UserStore
            assert UserStore.get_by_external("proxy", "proxy-sans-groupe") is None

    def test_break_glass_local_sans_auto_login(self, proxy_app):
        with proxy_app.test_client() as client:
            r = client.get("/login?local=1", headers=_HEADERS_BOSS)
            assert r.status_code == 200                            # pas de redirection
            assert 'name="password"' in r.get_data(as_text=True)

    def test_logout_ne_reconnecte_pas_immediatement(self, proxy_app):
        with proxy_app.test_client() as client:
            client.get("/auth/proxy/login", headers=_HEADERS_BOSS)
            r = client.post("/logout")
            assert r.status_code == 302 and "local=1" in r.headers["Location"]


class TestAutoLoginDesactive:
    def test_page_login_montre_le_bouton(self, _pg_database):
        app = _make_app(_pg_database, proxy={"trusted_ips": ["127.0.0.1"], "auto_login": False})
        with app.test_client() as client:
            r = client.get("/login")
            assert r.status_code == 200                            # pas de redirection
            assert "/auth/proxy/login" in r.get_data(as_text=True)  # bouton SSO

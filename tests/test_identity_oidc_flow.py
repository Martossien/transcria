"""Chantier identité lot 1 — flux OIDC COMPLET contre un IdP factice réel.

`oidc-provider-mock` sert un vrai serveur OIDC (découverte, JWKS, authorize,
token) dans un thread : ces tests déroulent le flux Authorization Code + PKCE
de bout en bout à travers les routes du portail — le protocole n'est PAS mocké.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import oidc_provider_mock
import pytest
import requests
from oidc_provider_mock import User as IdpUser

from transcria.config import _deep_merge, load_config

_MAPPING = {"claim": "groups",
            "rules": [{"group": "transcria-admins", "role": "admin"},
                      {"group": "transcria-lecteurs", "role": "viewer"}],
            "default": "deny"}


@pytest.fixture(scope="module")
def idp():
    users = [
        IdpUser(sub="sub-admin", claims={"email": "boss@corp.example", "name": "Boss Corp",
                                         "preferred_username": "boss",
                                         "groups": ["autre", "transcria-admins"]}),
        IdpUser(sub="sub-sans-groupe", claims={"email": "intrus@corp.example",
                                               "preferred_username": "intrus", "groups": []}),
    ]
    with oidc_provider_mock.run_server_in_thread(user_claims=users) as server:
        yield f"http://localhost:{server.server_port}"


@pytest.fixture(scope="module")
def oidc_app(idp, _pg_database):
    os.environ["TRANSCRIA_DATABASE_URL"] = _pg_database
    cfg = load_config()
    cfg = _deep_merge(cfg, {
        "auth": {"backend": "oidc",
                 # Même graine admin que conftest : cette app démarre AVANT la
                 # fixture de session `app`, et ensure_admin (au boot) sème le
                 # premier admin — il doit porter le mot de passe de la suite.
                 "first_admin_password": "admin-change-me",
                 "oidc": {"issuer": idp, "client_id": "transcria-tests",
                          "client_secret": "secret-tests"},
                 "role_mapping": _MAPPING},
        "storage": {"jobs_dir": "/tmp/transcria-oidc-tests-jobs"},
    })
    from app import create_app
    app = create_app(config=cfg, start_background_services=False)
    app.config.update({"TESTING": True, "SERVER_NAME": "localhost.test"})
    return app


def _sso_dance(client, idp, sub: str):
    """Déroule login → consentement IdP (sub choisi) → callback. Retourne la réponse callback."""
    r = client.get("/auth/oidc/login")
    assert r.status_code == 302 and r.headers["Location"].startswith(idp)
    idp_resp = requests.post(r.headers["Location"], data={"sub": sub}, allow_redirects=False, timeout=10)
    assert idp_resp.status_code == 302
    cb = urlparse(idp_resp.headers["Location"])
    qs = parse_qs(cb.query)
    return client.get(f"{cb.path}?code={qs['code'][0]}&state={qs['state'][0]}")


class TestFluxComplet:
    def test_login_jit_role_et_session(self, oidc_app, idp):
        with oidc_app.test_client() as client:
            r = _sso_dance(client, idp, "sub-admin")
            assert r.status_code == 302 and r.headers["Location"].endswith("/")
            with oidc_app.app_context():
                from transcria.auth.store import UserStore
                u = UserStore.get_by_external("oidc", "sub-admin")
                assert u is not None and u.role == "admin" and u.username == "boss"
                assert u.email == "boss@corp.example"
            # La session vaut login : une page protégée répond sans redirection.
            assert client.get("/").status_code == 200

    def test_refus_sans_groupe_mappe(self, oidc_app, idp):
        with oidc_app.test_client() as client:
            r = _sso_dance(client, idp, "sub-sans-groupe")
            assert r.status_code == 403
            assert "Accès non attribué" in r.get_data(as_text=True)
            with oidc_app.app_context():
                from transcria.auth.store import UserStore
                assert UserStore.get_by_external("oidc", "sub-sans-groupe") is None  # rien créé

    def test_state_falsifie_refuse(self, oidc_app, idp):
        with oidc_app.test_client() as client:
            r = client.get("/auth/oidc/login")
            idp_resp = requests.post(r.headers["Location"], data={"sub": "sub-admin"},
                                     allow_redirects=False, timeout=10)
            qs = parse_qs(urlparse(idp_resp.headers["Location"]).query)
            r2 = client.get(f"/auth/oidc/callback?code={qs['code'][0]}&state=FALSIFIE")
            assert r2.status_code == 401

    def test_break_glass_formulaire_local_actif(self, oidc_app):
        """Backend oidc actif : /login?local=1 sert le formulaire, et les comptes
        FÉDÉRÉS y échouent par construction (hachage inutilisable)."""
        with oidc_app.test_client() as client:
            page = client.get("/login").get_data(as_text=True)
            assert "auth/oidc/login" in page                # bouton SSO
            assert 'name="password"' not in page            # formulaire masqué par défaut
            page_local = client.get("/login?local=1").get_data(as_text=True)
            assert 'name="password"' in page_local          # break-glass accessible
            r = client.post("/login", data={"username": "boss", "password": "x" * 24})
            assert r.status_code == 401                     # compte fédéré : refus mdp


class TestIdpEnPanne:
    def test_idp_injoignable_message_et_break_glass(self, _pg_database):
        os.environ["TRANSCRIA_DATABASE_URL"] = _pg_database
        cfg = load_config()
        cfg = _deep_merge(cfg, {
            "auth": {"backend": "oidc",
                     "first_admin_password": "admin-change-me",  # même graine que conftest
                     "oidc": {"issuer": "http://127.0.0.1:9",  # port fermé
                              "client_id": "x", "client_secret": "y"},
                     "role_mapping": _MAPPING}})
        from app import create_app
        app = create_app(config=cfg, start_background_services=False)
        app.config.update({"TESTING": True})
        with app.test_client() as client:
            r = client.get("/auth/oidc/login")
            assert r.status_code == 503
            body = r.get_data(as_text=True)
            assert "indisponible" in body and 'name="password"' in body  # break-glass montré

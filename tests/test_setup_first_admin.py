"""Parcours « première visite = création du compte admin » (issue #11 v2).

Sur base vierge (backend local), le portail impose /setup : plus de secret généré à
retrouver dans un journal — l'utilisateur choisit son mot de passe là où il ne peut
pas le rater. La page se verrouille dès qu'un compte existe.
"""
from __future__ import annotations

import pytest

from transcria.auth.models import Role, User
from transcria.auth.store import UserStore
from transcria.database import db

# Helpers dupliqués de test_auth_store.py (tests/ n'est pas un package importable) :
# vidage FK-safe + snapshot/restore pour simuler une base vierge sans casser la
# fixture `app` de session.
_USER_FIELDS = ("id", "username", "display_name", "email", "password_hash", "role",
                "is_active", "created_at", "last_login")


def _empty_all_tables():
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


def _snapshot_users():
    return [{f: getattr(u, f) for f in _USER_FIELDS} for u in db.session.query(User).all()]


def _restore_users(snapshot):
    _empty_all_tables()
    for data in snapshot:
        db.session.add(User(**data))
    db.session.commit()


@pytest.fixture()
def client_base_vierge(app):
    with app.app_context():
        snapshot = _snapshot_users()
        _empty_all_tables()
    try:
        # PAS de `with app.test_client()` : le gestionnaire de contexte PRÉSERVE le
        # contexte de requête entre les appels, et un second client créé dedans
        # hériterait du current_user du premier (vécu : le client « anonyme » du test
        # de verrouillage arrivait authentifié).
        yield app.test_client()
    finally:
        with app.app_context():
            _restore_users(snapshot)


class TestPageSetup:
    def test_toute_visite_anonyme_mene_au_setup(self, client_base_vierge):
        r = client_base_vierge.get("/login")
        assert r.status_code == 302
        assert "/setup" in r.headers["Location"]

    def test_get_setup_affiche_le_formulaire(self, client_base_vierge):
        r = client_base_vierge.get("/setup")
        assert r.status_code == 200
        # Marqueurs stables (indépendants de la locale) : les champs du formulaire.
        assert b'name="username"' in r.data
        assert b'name="password"' in r.data
        assert b'name="password_confirm"' in r.data

    def test_creation_autologin_puis_verrouillage(self, client_base_vierge):
        r = client_base_vierge.post("/setup", data={
            "username": "chef", "password": "SuperSecret9", "password_confirm": "SuperSecret9",
        })
        assert r.status_code == 302

        with client_base_vierge.application.app_context():
            user = UserStore.get_by_username("chef")
            assert user is not None
            assert user.role_enum == Role.ADMIN
            assert user.check_password("SuperSecret9")
            assert UserStore.count_users() == 1

        # Auto-login : la session issue du POST accède au portail sans repasser par /login.
        assert client_base_vierge.get("/").status_code == 200

        # Verrouillage : un NOUVEAU visiteur anonyme n'a plus de page setup.
        anonyme = client_base_vierge.application.test_client()
        r2 = anonyme.get("/setup")
        assert r2.status_code == 302
        assert "/login" in r2.headers["Location"]
        # Et /login redevient une vraie page de connexion (plus de redirection setup).
        assert anonyme.get("/login").status_code == 200

    def test_mot_de_passe_trop_court_refuse(self, client_base_vierge):
        r = client_base_vierge.post("/setup", data={
            "username": "chef", "password": "court", "password_confirm": "court",
        })
        assert r.status_code == 400
        with client_base_vierge.application.app_context():
            assert UserStore.count_users() == 0

    def test_confirmation_differente_refusee(self, client_base_vierge):
        r = client_base_vierge.post("/setup", data={
            "username": "chef", "password": "SuperSecret9", "password_confirm": "Autre-chose9",
        })
        assert r.status_code == 400
        with client_base_vierge.application.app_context():
            assert UserStore.count_users() == 0

    def test_backend_federe_jamais_de_setup(self, client_base_vierge, monkeypatch):
        # Les déploiements SSO provisionnent leurs comptes au premier login fédéré :
        # la page setup ne doit ni s'afficher ni intercepter /login.
        import transcria.auth.routes as auth_routes

        monkeypatch.setattr(auth_routes, "identity_backend_name", lambda cfg: "oidc")
        r = client_base_vierge.get("/setup")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_username_vide_retombe_sur_admin(self, client_base_vierge):
        r = client_base_vierge.post("/setup", data={
            "username": "   ", "password": "SuperSecret9", "password_confirm": "SuperSecret9",
        })
        assert r.status_code == 302
        with client_base_vierge.application.app_context():
            assert UserStore.get_by_username("admin") is not None

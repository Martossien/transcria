"""Chantier identité lot 0 — socle enfichable (docs/GESTION_IDENTITE.md §3.1).

Le comportement LOCAL est le golden : les 34 tests d'auth existants passent par
le dispatch sans modification — ce fichier ne teste que ce que le lot AJOUTE."""
from __future__ import annotations

import pytest

from transcria.auth.identity import LocalBackend, get_identity_backend, identity_backend_name


class TestResolution:
    def test_defaut_local(self):
        assert identity_backend_name({}) == "local"
        assert isinstance(get_identity_backend({}), LocalBackend)
        assert isinstance(get_identity_backend({"auth": {"backend": "local"}}), LocalBackend)

    def test_backend_inconnu_refuse_jamais_de_repli(self):
        """Un admin qui croit son SSO actif ne doit JAMAIS servir du local sans le savoir."""
        with pytest.raises(ValueError, match="non disponible"):
            get_identity_backend({"auth": {"backend": "oidc"}})
        with pytest.raises(ValueError):
            get_identity_backend({"auth": {"backend": "n_importe_quoi"}})

    def test_schema_refuse_backend_non_livre(self):
        from copy import deepcopy

        from transcria.config.config_schema import validate_config
        from transcria.config.loader import get_default_config

        cfg = deepcopy(get_default_config())
        cfg["auth"]["backend"] = "oidc"
        result = validate_config(cfg)
        assert any("auth.backend" in e for e in result.errors)


class TestComptesFederes:
    def test_get_by_external_et_veto_mot_de_passe(self, app):
        from transcria.auth.models import Role, db
        from transcria.auth.store import UserStore

        with app.app_context():
            u = UserStore.create_user("sso-alice", "x" * 24, role=Role.OPERATOR)
            u.identity_source = "oidc"
            u.external_subject = "sub-abc-123"
            db.session.commit()
            uid = u.id

            found = UserStore.get_by_external("oidc", "sub-abc-123")
            assert found is not None and found.id == uid
            assert UserStore.get_by_external("ldap", "sub-abc-123") is None
            assert UserStore.get_by_external("oidc", "") is None

            # Veto : le mot de passe d'un compte fédéré se gère chez le fournisseur.
            assert UserStore.change_password(uid, "y" * 24) is False

    def test_compte_local_change_password_intact(self, app):
        from transcria.auth.models import Role
        from transcria.auth.store import UserStore

        with app.app_context():
            u = UserStore.create_user("local-bob", "x" * 24, role=Role.OPERATOR)
            assert (u.identity_source or "local") == "local"
            assert UserStore.change_password(u.id, "y" * 24) is True

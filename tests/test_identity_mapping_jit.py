"""Chantier identité lot 1 — mapping groupes→rôles et JIT (les invariants sécurité).

Chaque invariant de docs/GESTION_IDENTITE.md §3.5-3.6 a son test — c'est le
module le plus sensible du chantier (élévation, rétrogradation, veto, collisions).
"""
from __future__ import annotations

import pytest

from transcria.auth.identity.base import FederatedIdentity
from transcria.auth.identity.jit import (
    UNUSABLE_PASSWORD_HASH,
    FederatedLoginDenied,
    provision_federated,
)
from transcria.auth.identity.mapping import resolve_role, validate_role_mapping
from transcria.auth.models import Role

_MAPPING = {
    "rules": [
        {"group": "transcria-admins", "role": "admin"},
        {"group": "CN=Transcria Users,OU=Apps,DC=corp", "role": "operator"},
    ],
    "default": "deny",
}


class TestMapping:
    def test_premier_match_gagne_egalite_stricte(self):
        d = resolve_role(("autre", "transcria-admins", "CN=Transcria Users,OU=Apps,DC=corp"), _MAPPING)
        assert d.role is Role.ADMIN and d.matched_group == "transcria-admins"
        # Égalité STRICTE : préfixe/casse différente ne matche pas.
        assert resolve_role(("Transcria-Admins",), _MAPPING).denied
        assert resolve_role(("transcria-admins-bis",), _MAPPING).denied

    def test_default_deny_et_viewer(self):
        d = resolve_role(("aucun-groupe-connu",), _MAPPING)
        assert d.denied and d.received_groups == ("aucun-groupe-connu",)  # pour l'audit
        d2 = resolve_role((), {**_MAPPING, "default": "viewer"})
        assert d2.role is Role.VIEWER and d2.matched_group is None

    def test_validation_du_mapping(self):
        assert validate_role_mapping(_MAPPING) == []
        errs = validate_role_mapping({"default": "operator"})        # élévation implicite interdite
        assert any("default" in e for e in errs)
        errs = validate_role_mapping({"rules": [{"group": "g", "role": "superadmin"}]})
        assert any("inconnu" in e for e in errs)
        errs = validate_role_mapping({"rules": [{"role": "admin"}]})  # group manquant
        assert any("group" in e for e in errs)


def _identity(**kw) -> FederatedIdentity:
    base = dict(subject="sub-1", username="jit-alice", display_name="Alice D",
                email="alice@corp.example", groups=("transcria-admins",), source="oidc")
    base.update(kw)
    return FederatedIdentity(**base)


def _cfg(default="deny"):
    return {"auth": {"role_mapping": {**_MAPPING, "default": default}}}


class TestJIT:
    def test_creation_puis_resynchronisation_et_retrogradation(self, app):
        with app.app_context():
            user, decision = provision_federated(_identity(subject="sub-resync", username="jit-resync"), _cfg())
            assert user.identity_source == "oidc" and user.external_subject == "sub-resync"
            assert user.role == "admin" and decision.matched_group == "transcria-admins"
            assert user.password_hash == UNUSABLE_PASSWORD_HASH
            assert user.check_password("n'importe quoi") is False   # par construction

            # Retiré du groupe admins → RÉTROGRADÉ au prochain login (jamais max()).
            user2, _ = provision_federated(
                _identity(subject="sub-resync", username="jit-resync", groups=("CN=Transcria Users,OU=Apps,DC=corp",)), _cfg())
            assert user2.id == user.id and user2.role == "operator"

    def test_refus_si_aucun_groupe_mappe(self, app):
        with app.app_context():
            with pytest.raises(FederatedLoginDenied) as exc:
                provision_federated(_identity(groups=("rien",)), _cfg())
            assert exc.value.decision.received_groups == ("rien",)   # l'audit verra les groupes

    def test_veto_compte_desactive_localement(self, app):
        from transcria.auth.models import db

        with app.app_context():
            user, _ = provision_federated(_identity(subject="sub-veto", username="jit-veto"), _cfg())
            user.is_active = False
            db.session.commit()
            with pytest.raises(FederatedLoginDenied, match="veto"):
                provision_federated(_identity(subject="sub-veto", username="jit-veto"), _cfg())

    def test_collision_username_jamais_d_ecrasement(self, app):
        from transcria.auth.store import UserStore

        with app.app_context():
            local = UserStore.create_user("jit-coll", "x" * 24, role=Role.OPERATOR)
            fed, _ = provision_federated(_identity(subject="sub-autre", username="jit-coll"), _cfg())
            assert fed.username == "jit-coll@oidc"                      # suffixé, pas écrasé
            assert UserStore.get_by_username("jit-coll").id == local.id  # le local intact
            fed2, _ = provision_federated(
                _identity(subject="sub-encore", username="jit-coll", source="oidc"), _cfg())
            assert fed2.username == "jit-coll@oidc-2"

    def test_mot_de_passe_federe_reste_impossible(self, app):
        from transcria.auth.store import UserStore

        with app.app_context():
            user, _ = provision_federated(_identity(subject="sub-pwd", username="jit-pwd"), _cfg())
            assert UserStore.change_password(user.id, "y" * 24) is False

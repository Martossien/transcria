"""Chantier identité lot 2 — connecteur LDAP / Active Directory.

Le module le plus sensible du chantier (mots de passe manipulés en direct). La
couverture combine :

- les HELPERS PURS (validation d'identifiant, échappement de filtre anti-injection,
  décodage des codes de résultat AD) — sans aucun LDAP ;
- le FLUX COMPLET contre un vrai serveur mock ldap3 (``MOCK_SYNC``) : bind du
  compte de service, recherche, re-bind utilisateur, lecture des groupes — cela
  PROUVE mon usage réel de l'API ldap3, pas seulement ma logique ;
- des STUBS ciblés pour ce que le mock ne peut pas produire : codes de résultat
  AD (compte désactivé/expiré/verrouillé), annuaire injoignable, groupes
  imbriqués, filtre ambigu.
"""
from __future__ import annotations

import types

import pytest
from ldap3 import MOCK_SYNC, Connection, Server
from ldap3.core.exceptions import LDAPSocketOpenError

from transcria.auth.identity.base import FederatedIdentity, IdentityUnavailable
from transcria.auth.identity.ldap import (
    LdapBackend,
    ad_error_reason,
    build_user_filter,
    resolve_service_password,
    validate_username,
)

_SVC_DN = "CN=svc,DC=corp"
_ALICE_DN = "CN=Alice,OU=Users,DC=corp"
_ADMIN_GROUP = "CN=Transcria Admins,OU=Groups,DC=corp"

_MAPPING = {"rules": [{"group": _ADMIN_GROUP, "role": "admin"},
                      {"group": "CN=Lecteurs,OU=Groups,DC=corp", "role": "viewer"}],
            "default": "deny"}


# ─────────────────────────── helpers purs ────────────────────────────────
class TestHelpersPurs:
    def test_validate_username(self):
        assert validate_username("jdupont")
        assert validate_username("CORP\\jdupont")
        assert not validate_username("")
        assert not validate_username("a" * 257)
        assert not validate_username("nul\x00byte")
        assert not validate_username("ctrl\x01char")
        assert not validate_username("tab\tsep")

    def test_build_user_filter_echappe_injection(self):
        from ldap3.utils.conv import escape_filter_chars

        payload = "a)(uid=*))(|(x="
        flt = build_user_filter("(&(objectClass=user)(sAMAccountName={username}))", payload)
        # La charge utile n'apparaît QUE sous sa forme échappée (aucun caractère de
        # filtre brut issu de l'entrée ne subsiste → injection neutralisée).
        escaped = escape_filter_chars(payload)
        assert escaped in flt
        assert payload not in flt
        assert "\\28" in escaped and "\\29" in escaped and "\\2a" in escaped
        assert build_user_filter("(cn={username})", "bob") == "(cn=bob)"

    def test_ad_error_reason(self):
        assert ad_error_reason({"message": "80090308: LdapErr: DSID-0C09, data 52e, v2580"}) == "bad_password"
        assert ad_error_reason({"message": "..., data 533, ..."}) == "account_disabled"
        assert ad_error_reason({"message": "..., data 701, ..."}) == "account_expired"
        assert ad_error_reason({"message": "..., data 775, ..."}) == "account_locked"
        assert ad_error_reason({"message": "..., data 532, ..."}) == "password_expired"
        assert ad_error_reason({"message": "..., data 525, ..."}) == "user_not_found"
        assert ad_error_reason({"message": "quelque chose d'autre"}) == "invalid_credentials"
        assert ad_error_reason(None) == "invalid_credentials"

    def test_resolve_service_password_env_prioritaire(self, monkeypatch):
        monkeypatch.setenv("TEST_LDAP_PW", "depuis-env")
        assert resolve_service_password({"service_password_env": "TEST_LDAP_PW",
                                         "service_password": "en-clair"}) == "depuis-env"
        assert resolve_service_password({"service_password": "en-clair"}) == "en-clair"
        assert resolve_service_password({"service_password_env": "ABSENTE"}) == ""


# ─────────────────── flux complet contre le mock ldap3 ────────────────────
def _mock_entries(guid="guid-alice", groups=None, sam="alice"):
    groups = groups if groups is not None else [_ADMIN_GROUP]
    return {
        _SVC_DN: {"userPassword": "svcpw", "objectClass": "person"},
        _ALICE_DN: {"userPassword": "alicepw", "objectClass": "user",
                    "sAMAccountName": sam, "displayName": "Alice Test", "mail": "alice@corp.example",
                    "memberOf": groups, "objectGUID": guid},
    }


def _mock_factory(entries):
    """Fabrique de connexions MOCK_SYNC — chaque connexion reçoit le même annuaire
    (le bind valide le mot de passe contre userPassword, la recherche lit tout)."""
    def factory(server, user, password):
        conn = Connection(Server("mock"), user=user, password=password,
                          client_strategy=MOCK_SYNC, raise_exceptions=False)
        for dn, attrs in entries.items():
            conn.strategy.add_entry(dn, dict(attrs))
        return conn
    return factory


def _cfg(**over):
    ldap = {"servers": ["ldaps://mock.corp"], "bind_mode": "service", "service_dn": _SVC_DN,
            "service_password": "svcpw", "base_dn": "DC=corp",
            "user_filter": "(sAMAccountName={username})"}
    ldap.update(over)
    return {"auth": {"backend": "ldap", "ldap": ldap, "role_mapping": _MAPPING}}


class TestFluxServiceMock:
    def test_login_nominal_identite_complete(self):
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(_mock_entries()))
        ident = b.authenticate("alice", "alicepw")
        assert isinstance(ident, FederatedIdentity)
        assert ident.source == "ldap" and ident.subject == "guid-alice"
        assert ident.username == "alice" and ident.display_name == "Alice Test"
        assert ident.email == "alice@corp.example"
        assert ident.groups == (_ADMIN_GROUP,)

    def test_mauvais_mot_de_passe_refuse(self):
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(_mock_entries()))
        assert b.authenticate("alice", "MAUVAIS") is None
        assert b.last_failure_reason == "invalid_credentials"   # le mock ne code pas AD

    def test_utilisateur_introuvable_refuse(self):
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(_mock_entries()))
        assert b.authenticate("fantome", "peu-importe") is None

    def test_mot_de_passe_vide_jamais_de_bind(self):
        """Bind non authentifié (RFC 4513) : un mot de passe vide est refusé AVANT
        toute tentative — jamais de contournement par bind anonyme."""
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(_mock_entries()))
        assert b.authenticate("alice", "") is None

    def test_username_hostile_refuse_sans_io(self):
        called = []

        def spy(server, user, password):
            called.append(user)
            raise AssertionError("aucune connexion ne doit s'ouvrir")

        b = LdapBackend(_cfg(), connection_factory=spy)
        assert b.authenticate("bad\x00null", "x") is None
        assert called == []

    def test_compte_service_refuse_est_indisponibilite(self):
        # Mauvais mot de passe de service → personne ne peut se connecter = INDISPO,
        # pas un refus d'identifiant utilisateur.
        b = LdapBackend(_cfg(service_password="MAUVAIS"), connection_factory=_mock_factory(_mock_entries()))
        with pytest.raises(IdentityUnavailable):
            b.authenticate("alice", "alicepw")

    def test_service_password_vide_est_indisponibilite_sans_bind(self):
        # Mot de passe de service vide (ex. variable d'env absente) : diagnostic
        # précis AVANT tout bind — jamais de bind anonyme silencieux.
        def spy(server, user, password):
            raise AssertionError("aucun bind ne doit être tenté")

        b = LdapBackend(_cfg(service_password="", service_password_env=""), connection_factory=spy)
        with pytest.raises(IdentityUnavailable):
            b.authenticate("alice", "alicepw")

    def test_objectguid_binaire_devient_hex(self):
        entries = _mock_entries(guid=b"\x01\x02\xab\xcd")
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(entries))
        ident = b.authenticate("alice", "alicepw")
        assert ident.subject == "0102abcd"          # bytes → hex déterministe

    def test_sans_groupe_mappe_identite_avec_groupes_bruts(self):
        # Le connecteur ne décide PAS du rôle : il rapporte les groupes tels quels,
        # le mapping/JIT tranche ensuite (ici un groupe non mappé).
        entries = _mock_entries(groups=["CN=Autre,DC=corp"])
        b = LdapBackend(_cfg(), connection_factory=_mock_factory(entries))
        ident = b.authenticate("alice", "alicepw")
        assert ident.groups == ("CN=Autre,DC=corp",)


class TestTls:
    def test_ldaps_valide_le_certificat(self):
        """Invariant de sécurité : ldap3 avec use_ssl et tls=None n'active AUCUNE
        validation (CERT_NONE, MITM silencieux). On impose toujours CERT_REQUIRED."""
        import ssl

        srv = LdapBackend(_cfg(servers=["ldaps://dc1.corp"], use_ssl=True))._build_server()
        assert srv.tls is not None and srv.tls.validate == ssl.CERT_REQUIRED

    def test_start_tls_valide_aussi_le_certificat(self):
        import ssl

        srv = LdapBackend(_cfg(servers=["ldap://dc1.corp"], use_ssl=False,
                               start_tls=True))._build_server()
        assert srv.tls.validate == ssl.CERT_REQUIRED


class TestFluxDirectMock:
    def test_bind_direct_nominal(self):
        entries = {
            "CN=bob,OU=Users,DC=corp": {"userPassword": "bobpw", "objectClass": "user",
                                        "sAMAccountName": "bob", "displayName": "Bob",
                                        "mail": "bob@corp", "memberOf": [_ADMIN_GROUP],
                                        "objectGUID": "guid-bob"},
        }
        cfg = _cfg(bind_mode="direct", user_dn_template="CN={username},OU=Users,DC=corp")
        b = LdapBackend(cfg, connection_factory=_mock_factory(entries))
        ident = b.authenticate("bob", "bobpw")
        assert ident is not None and ident.username == "bob" and ident.groups == (_ADMIN_GROUP,)

    def test_bind_direct_mauvais_mot_de_passe(self):
        entries = {"CN=bob,OU=Users,DC=corp": {"userPassword": "bobpw", "objectClass": "user"}}
        cfg = _cfg(bind_mode="direct", user_dn_template="CN={username},OU=Users,DC=corp")
        b = LdapBackend(cfg, connection_factory=_mock_factory(entries))
        assert b.authenticate("bob", "MAUVAIS") is None

    def test_bind_direct_sans_template_est_indisponibilite(self):
        cfg = _cfg(bind_mode="direct", user_dn_template="")
        b = LdapBackend(cfg, connection_factory=_mock_factory({}))
        with pytest.raises(IdentityUnavailable):
            b.authenticate("bob", "bobpw")


# ─────────────── stubs ciblés : codes AD, indispo, imbriqués ──────────────
class _FakeEntry:
    def __init__(self, dn, attrs):
        self.entry_dn = dn
        self._a = attrs

    def __getitem__(self, name):
        return types.SimpleNamespace(values=list(self._a.get(name, [])))


class _StubConn:
    """Connexion factice : contrôle total du bind, de la recherche et du résultat."""

    def __init__(self, *, bind_ok=True, message="", user_entries=None, nested_entries=None, bind_raises=None):
        self._bind_ok = bind_ok
        self._bind_raises = bind_raises
        self.result = {"message": message, "description": "x"}
        self._user_entries = user_entries or []
        self._nested_entries = nested_entries or []
        self.entries = []
        self.unbound = False

    def start_tls(self):
        pass

    def bind(self):
        if self._bind_raises:
            raise self._bind_raises
        return self._bind_ok

    def search(self, base, flt, **kwargs):
        # La recherche imbriquée (règle en chaîne AD) renvoie les groupes ; sinon l'utilisateur.
        self.entries = self._nested_entries if "1.2.840.113556.1.4.1941" in flt else self._user_entries

    def unbind(self):
        self.unbound = True


def _dispatch_factory(user_stub_kwargs):
    """Compte de service → bind ok + recherche de l'utilisateur ; utilisateur → stub paramétré."""
    entry = _FakeEntry(_ALICE_DN, {"sAMAccountName": ["alice"], "displayName": ["Alice"],
                                   "mail": ["alice@corp"], "objectGUID": ["guid-alice"],
                                   "memberOf": [_ADMIN_GROUP]})

    def factory(server, user, password):
        if user == _SVC_DN:
            return _StubConn(bind_ok=True, user_entries=[entry],
                             nested_entries=[_FakeEntry(_ADMIN_GROUP, {})])
        return _StubConn(**user_stub_kwargs)
    return factory


class TestCodesAD:
    @pytest.mark.parametrize("data_code,reason", [
        ("533", "account_disabled"), ("701", "account_expired"),
        ("775", "account_locked"), ("52e", "bad_password"), ("532", "password_expired"),
    ])
    def test_bind_utilisateur_refuse_code_ad_dans_audit(self, data_code, reason):
        f = _dispatch_factory({"bind_ok": False, "message": f"80090308: ..., data {data_code}, v2580"})
        b = LdapBackend(_cfg(), connection_factory=f)
        assert b.authenticate("alice", "x") is None
        # L'utilisateur ne voit qu'un message générique ; l'ADMIN a la cause précise.
        assert b.last_failure_reason == reason


class TestIndisponibilite:
    def test_socket_injoignable_leve_identity_unavailable(self):
        def factory(server, user, password):
            return _StubConn(bind_raises=LDAPSocketOpenError("controller down"))

        b = LdapBackend(_cfg(), connection_factory=factory)
        with pytest.raises(IdentityUnavailable):
            b.authenticate("alice", "alicepw")


class TestGroupesImbriques:
    def test_resolution_imbriquee_utilise_la_regle_en_chaine(self):
        f = _dispatch_factory({"bind_ok": True})
        b = LdapBackend(_cfg(resolve_nested_groups=True), connection_factory=f)
        ident = b.authenticate("alice", "alicepw")
        # La recherche imbriquée a renvoyé le groupe admin (via l'OID en chaîne).
        assert ident is not None and ident.groups == (_ADMIN_GROUP,)

    def test_filtre_ambigu_refuse(self):
        entry_a = _FakeEntry(_ALICE_DN, {"sAMAccountName": ["alice"]})
        entry_b = _FakeEntry("CN=Alice2,DC=corp", {"sAMAccountName": ["alice"]})

        def factory(server, user, password):
            if user == _SVC_DN:
                return _StubConn(bind_ok=True, user_entries=[entry_a, entry_b])
            return _StubConn(bind_ok=True)

        b = LdapBackend(_cfg(), connection_factory=factory)
        assert b.authenticate("alice", "x") is None       # 2 entrées = refus sûr


# ───────────── intégration route login (LDAP → JIT → session) ─────────────
class _RouteBackend:
    """Faux backend LDAP posé sur la route (pas de socket) : la route pilote le
    rate-limit, le JIT, la session et l'audit — c'est CELA qu'on teste ici."""

    source = "ldap"

    def __init__(self, *, identity=None, unavailable=False, reason=None):
        self._identity = identity
        self._unavailable = unavailable
        self.last_failure_reason = reason

    def authenticate(self, username, password):
        if self._unavailable:
            raise IdentityUnavailable("controller down")
        return self._identity


def _route_cfg():
    return {"auth": {"backend": "ldap", "role_mapping": _MAPPING,
                     "ldap": {"servers": ["ldaps://x"]}, "oidc": {}}}


class TestRouteLdap:
    def test_login_ldap_provisionne_et_ouvre_session(self, app, monkeypatch):
        ident = FederatedIdentity(subject="ldap-route-1", username="ldaproute",
                                  display_name="LDAP Route", email="lr@corp.example",
                                  groups=(_ADMIN_GROUP,), source="ldap")
        monkeypatch.setattr("transcria.auth.routes.get_password_backend",
                            lambda cfg: _RouteBackend(identity=ident))
        monkeypatch.setattr("transcria.auth.routes.get_config", _route_cfg)
        with app.test_client() as client:
            r = client.post("/login", data={"username": "ldaproute", "password": "pw"})
            assert r.status_code == 302
            with app.app_context():
                from transcria.auth.store import UserStore
                u = UserStore.get_by_external("ldap", "ldap-route-1")
                assert u is not None and u.role == "admin" and u.email == "lr@corp.example"
            assert client.get("/").status_code == 200      # la session vaut login

    def test_break_glass_local_court_circuite_ldap(self, app, monkeypatch):
        consulted = {"ldap": False}

        def gpb(cfg):
            consulted["ldap"] = True
            return _RouteBackend(identity=None)

        monkeypatch.setattr("transcria.auth.routes.get_password_backend", gpb)
        with app.test_client() as client:
            r = client.post("/login?local=1", data={"username": "admin", "password": "admin-change-me"})
            assert r.status_code == 302
            assert consulted["ldap"] is False              # ?local=1 force le backend LOCAL

    def test_annuaire_indisponible_503_avec_secours(self, app, monkeypatch):
        monkeypatch.setattr("transcria.auth.routes.get_password_backend",
                            lambda cfg: _RouteBackend(unavailable=True))
        monkeypatch.setattr("transcria.auth.routes.get_config", _route_cfg)
        with app.test_client() as client:
            r = client.post("/login", data={"username": "x", "password": "y"})
            assert r.status_code == 503
            body = r.get_data(as_text=True)
            assert "indisponible" in body and 'name="password"' in body

    def test_mapping_refuse_403_rien_cree(self, app, monkeypatch):
        ident = FederatedIdentity(subject="ldap-deny-1", username="denied", display_name="D",
                                  email="", groups=("CN=Inconnu,DC=corp",), source="ldap")
        monkeypatch.setattr("transcria.auth.routes.get_password_backend",
                            lambda cfg: _RouteBackend(identity=ident))
        monkeypatch.setattr("transcria.auth.routes.get_config", _route_cfg)
        with app.test_client() as client:
            r = client.post("/login", data={"username": "denied", "password": "pw"})
            assert r.status_code == 403
            assert "Accès non attribué" in r.get_data(as_text=True)
            with app.app_context():
                from transcria.auth.store import UserStore
                assert UserStore.get_by_external("ldap", "ldap-deny-1") is None

    def test_page_login_ldap_formulaire_natif_sans_bouton_sso(self, app, monkeypatch):
        monkeypatch.setattr("transcria.auth.routes.get_config", _route_cfg)
        with app.test_client() as client:
            page = client.get("/login").get_data(as_text=True)
            assert 'name="password"' in page                     # formulaire natif LDAP
            assert "identifiants d'entreprise" in page.lower()    # indice annuaire
            assert "auth/oidc/login" not in page                 # aucun bouton SSO

    def test_anti_bruteforce_ignore_x_forwarded_for(self, app):
        """Sécurité : X-Forwarded-For ne doit PAS contourner l'anti-bourrinage —
        sinon devinette illimitée d'un mot de passe (LDAP inclus) en tournant l'IP.
        La clé repose sur l'adresse socket (remote_addr)."""
        from transcria.auth.rate_limit import login_rate_limiter
        login_rate_limiter.reset()
        try:
            with app.test_client() as client:
                statuses = [
                    client.post("/login", data={"username": "admin", "password": "mauvais"},
                                headers={"X-Forwarded-For": f"9.9.9.{i}"},   # IP XFF différente à chaque coup
                                environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code
                    for i in range(6)
                ]
            # Malgré la rotation XFF, le seuil est atteint (même remote_addr) → 429.
            assert 429 in statuses
            assert statuses[-1] == 429
        finally:
            login_rate_limiter.reset()

    def test_compte_federe_refuse_au_formulaire_local(self, app, monkeypatch):
        with app.app_context():
            from transcria.auth.identity.jit import provision_federated
            ident = FederatedIdentity(subject="ldap-bg-1", username="bgfedere", display_name="BG",
                                      email="", groups=(_ADMIN_GROUP,), source="ldap")
            provision_federated(ident, _route_cfg())
        with app.test_client() as client:
            # Break-glass = comptes LOCAUX seulement : un compte fédéré (hachage
            # inutilisable) échoue par construction au formulaire local.
            r = client.post("/login?local=1", data={"username": "bgfedere", "password": "x" * 16})
            assert r.status_code == 401

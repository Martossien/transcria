"""Connecteur LDAP / Active Directory direct — docs/GESTION_IDENTITE.md §3.4 (lot 2).

À la différence d'OIDC et du proxy, LDAP est un backend À MOT DE PASSE : il passe
par le formulaire de connexion classique (identifiant + mot de passe) puis prouve
ces identifiants contre l'annuaire. Il produit ensuite une ``FederatedIdentity``
qui suit le MÊME provisionnement JIT et le MÊME mapping groupes→rôles que les
autres connecteurs (le rôle reste piloté par l'annuaire, jamais stocké en dur).

Décisions de sécurité verrouillées par le plan :

- **canal chiffré obligatoire** : LDAPS (``use_ssl``) ou StartTLS. Un annuaire en
  clair est REFUSÉ au boot (validation de schéma) sauf ``allow_plaintext: true``
  posé explicitement — un mot de passe ne transite jamais en clair par accident ;
- **mot de passe vide REFUSÉ** avant tout bind : un bind « non authentifié »
  (RFC 4513 §5.1.2, DN présent + mot de passe vide) réussit sur beaucoup de
  serveurs sans vérifier quoi que ce soit — c'est un contournement classique ;
- **échappement systématique** de l'entrée utilisateur dans les filtres
  (``escape_filter_chars``) : la seule défense qui compte contre l'injection LDAP ;
- **le compte de service lit, l'utilisateur prouve** : tous les attributs et les
  groupes sont lus sur la connexion de SERVICE (droits de lecture) ; le bind
  utilisateur ne sert QU'À valider le mot de passe, puis on se déconnecte ;
- ``auto_referrals=False`` : les renvois (referrals) AD multi-domaines sont une
  source classique de suspensions mystérieuses — jamais suivis silencieusement ;
- codes de résultat AD (52e/533/701/775…) distingués pour l'AUDIT de l'admin ;
  l'utilisateur ne voit qu'un message générique (anti-énumération).

Toute la mécanique ldap3 passe par une fabrique de connexion injectable
(``connection_factory``) : les tests substituent un annuaire déterministe (mock
ldap3 réel pour le flux, faux ciblé pour les codes AD et les pannes réseau) sans
qu'aucun socket ne s'ouvre.
"""
from __future__ import annotations

import logging

from ldap3 import FIRST, SUBTREE, Connection, Server, ServerPool, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from transcria.auth.identity.base import FederatedIdentity, IdentityUnavailable

logger = logging.getLogger(__name__)

# OID LDAP_MATCHING_RULE_IN_CHAIN : appartenance transitive (groupes imbriqués),
# évalué RÉCURSIVEMENT côté serveur AD — coûteux, activé sur option explicite.
_MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"

# Codes de diagnostic AD (sous-code « data <hex> » du résultat 49 invalidCredentials).
_AD_DATA_REASONS = {
    "525": "user_not_found",
    "52e": "bad_password",
    "530": "not_permitted_this_time",
    "531": "not_permitted_this_workstation",
    "532": "password_expired",
    "533": "account_disabled",
    "701": "account_expired",
    "773": "must_reset_password",
    "775": "account_locked",
}


def ldap_config(config: dict) -> dict:
    return ((config.get("auth", {}) or {}).get("ldap", {}) or {})


def resolve_service_password(cfg: dict) -> str:
    """``service_password_env`` (nom de variable) prime sur ``service_password``."""
    import os

    env_name = str(cfg.get("service_password_env") or "").strip()
    if env_name:
        return os.environ.get(env_name, "")
    return str(cfg.get("service_password") or "")


def validate_username(username: str) -> bool:
    """Garde d'entrée AVANT tout usage LDAP : rejette ce qui n'a rien à faire dans
    un identifiant (vide, trop long, caractères de contrôle/NUL). L'échappement de
    filtre neutralise l'injection de recherche ; cette garde ferme en amont les
    entrées manifestement hostiles, y compris pour le bind direct (DN/UPN)."""
    if not username or len(username) > 256:
        return False
    return not any(ord(ch) < 0x20 or ch == "\x7f" for ch in username)


def build_user_filter(template: str, username: str) -> str:
    """Filtre de recherche du compte, entrée utilisateur ÉCHAPPÉE (anti-injection).

    ``template`` contient ``{username}`` ; on n'y injecte que la version échappée
    par ``escape_filter_chars`` (neutralise ``* ( ) \\ NUL``)."""
    return template.replace("{username}", escape_filter_chars(username))


def ad_error_reason(result: dict | None) -> str:
    """Traduit le résultat d'un bind refusé en cause DIAGNOSTIC (pour l'audit admin).

    AD encode la vraie cause dans « data <hex> » du message de diagnostic ; on la
    reconnaît, sinon on retombe sur ``invalid_credentials`` (annuaires non-AD)."""
    message = str((result or {}).get("message") or "")
    lowered = message.lower()
    for data_code, reason in _AD_DATA_REASONS.items():
        if f"data {data_code}" in lowered:
            return reason
    return "invalid_credentials"


class LdapBackend:
    """Backend à mot de passe adossé à un annuaire LDAP/AD (docs §3.4).

    ``authenticate`` retourne une ``FederatedIdentity`` (identifiants acceptés),
    ``None`` (refusés — la cause AD précise est dans ``last_failure_reason`` pour
    l'audit) ou lève ``IdentityUnavailable`` (annuaire injoignable)."""

    source = "ldap"

    def __init__(self, config: dict, *, connection_factory=None):
        self._cfg = ldap_config(config)
        # Dernière cause de refus (code AD) — lue par la route pour l'audit admin.
        # Instance créée par requête (get_password_backend) : aucun partage d'état.
        self.last_failure_reason: str | None = None
        self._factory = connection_factory or self._default_connection

    # ── configuration dérivée ────────────────────────────────────────────
    @property
    def _servers(self) -> list[str]:
        raw = self._cfg.get("servers") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(s).strip() for s in raw if str(s).strip()]

    @property
    def _use_ssl(self) -> bool:
        return bool(self._cfg.get("use_ssl", True))

    @property
    def _start_tls(self) -> bool:
        return bool(self._cfg.get("start_tls", False))

    def _default_connection(self, server, user: str, password: str) -> Connection:
        # raise_exceptions=False : un bind refusé revient en False (pas d'exception)
        # pour distinguer « mauvais mot de passe » d'« annuaire injoignable » (socket).
        return Connection(server, user=user, password=password, auto_bind=False,
                          auto_referrals=False, raise_exceptions=False,
                          receive_timeout=int(self._cfg.get("receive_timeout_s", 10)))

    def _build_server(self):
        import ssl

        tls = None
        if self._use_ssl or self._start_tls:
            ca = str(self._cfg.get("tls_ca_file") or "").strip() or None
            # CERT_REQUIRED : un certificat serveur invalide FAIT ÉCHOUER la connexion
            # (jamais de LDAPS « décoratif » qui accepterait un homme du milieu).
            tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca)
        connect_timeout = int(self._cfg.get("connect_timeout_s", 5))
        servers = [Server(uri, use_ssl=self._use_ssl, tls=tls, connect_timeout=connect_timeout)
                   for uri in self._servers]
        if not servers:
            raise IdentityUnavailable("auth.ldap.servers vide")
        if len(servers) == 1:
            return servers[0]
        # FIRST : on tente les contrôleurs de domaine dans l'ordre, bascule au suivant
        # si le premier est injoignable (haute disponibilité AD multi-DC).
        return ServerPool(servers, FIRST, active=True, exhaust=False)

    # ── ouverture de connexion (couture de test) ─────────────────────────
    def _open(self, user: str, password: str) -> tuple[Connection, bool]:
        """Ouvre une connexion et tente le bind.

        Retourne ``(conn, True)`` si le bind réussit, ``(conn, False)`` s'il est
        REFUSÉ (identifiants) ; lève ``IdentityUnavailable`` si le serveur est
        injoignable (socket/TLS) — jamais confondu avec un refus d'identifiants."""
        try:
            conn = self._factory(self._server, user, password)
            if self._start_tls and not self._use_ssl:
                conn.start_tls()
            ok = conn.bind()
        except LDAPException as exc:
            raise IdentityUnavailable(f"annuaire injoignable : {exc}") from exc
        return conn, bool(ok)

    # ── point d'entrée ───────────────────────────────────────────────────
    def authenticate(self, username: str, password: str) -> FederatedIdentity | None:
        self.last_failure_reason = None
        # Mot de passe vide → bind non authentifié (RFC 4513) : REFUS immédiat,
        # jamais de tentative de bind. Username hostile → refus sans I/O.
        if not password or not validate_username(username):
            return None
        self._server = self._build_server()
        bind_mode = str(self._cfg.get("bind_mode") or "service").strip().lower()
        if bind_mode == "direct":
            return self._authenticate_direct(username, password)
        return self._authenticate_service(username, password)

    # ── mode service + recherche (recommandé AD) ─────────────────────────
    def _authenticate_service(self, username: str, password: str) -> FederatedIdentity | None:
        service_dn = str(self._cfg.get("service_dn") or "").strip()
        service_password = resolve_service_password(self._cfg)
        if not service_dn or not service_password:
            # DN ou mot de passe de service vide (ex. variable d'env absente au runtime) :
            # diagnostic PRÉCIS plutôt qu'un bind anonyme ou une erreur ldap3 cryptique.
            logger.error("auth ldap : DN/mot de passe du compte de service manquant "
                         "(vérifier service_dn et service_password/service_password_env)")
            raise IdentityUnavailable("compte de service LDAP non configuré")
        svc, ok = self._open(service_dn, service_password)
        if not ok:
            # Compte de service refusé = personne ne peut se connecter : c'est une
            # INDISPONIBILITÉ (mauvaise config), pas un refus d'identifiant utilisateur.
            logger.error("auth ldap : bind du compte de service refusé (vérifier service_dn/mot de passe)")
            raise IdentityUnavailable("compte de service LDAP refusé")
        try:
            entry = self._search_user(svc, username)
            if entry is None:
                # Utilisateur introuvable = refus générique (anti-énumération sur le
                # message ET l'audit). Réserve connue et ACCEPTÉE : un compte existant
                # ajoute un bind réseau jetable, d'où un léger écart temporel — canal
                # secondaire de faible sévérité, non corrigé par un bind factice (qui
                # doublerait la charge annuaire et ouvrirait une amplification DoS).
                return None
            user_dn = entry.entry_dn
            # Prouver le mot de passe par un bind JETABLE (aucune lecture dessus).
            user_conn, user_ok = self._open(user_dn, password)
            self._safe_unbind(user_conn)
            if not user_ok:
                self.last_failure_reason = ad_error_reason(getattr(user_conn, "result", None))
                logger.warning("auth ldap : bind utilisateur refusé (%s) — cause=%s",
                               username, self.last_failure_reason)
                return None
            groups = self._resolve_groups(svc, entry, user_dn)
            return self._build_identity(entry, user_dn, groups)
        finally:
            self._safe_unbind(svc)

    # ── mode bind direct (simple, sans compte de service) ────────────────
    def _authenticate_direct(self, username: str, password: str) -> FederatedIdentity | None:
        template = str(self._cfg.get("user_dn_template") or "").strip()
        if "{username}" not in template:
            logger.error("auth ldap : bind_mode=direct exige auth.ldap.user_dn_template avec {username}")
            raise IdentityUnavailable("auth.ldap.user_dn_template manquant en mode direct")
        # Le username va dans un DN/UPN de bind, PAS dans un filtre de recherche :
        # le bind ne parse pas la syntaxe de filtre (l'injection de recherche n'y a
        # pas cours) ; la garde validate_username a déjà écarté les entrées hostiles.
        user_id = template.replace("{username}", username)
        conn, ok = self._open(user_id, password)
        if not ok:
            self.last_failure_reason = ad_error_reason(getattr(conn, "result", None))
            self._safe_unbind(conn)
            logger.warning("auth ldap : bind direct refusé (%s) — cause=%s",
                           username, self.last_failure_reason)
            return None
        try:
            base_dn = str(self._cfg.get("base_dn") or "").strip()
            # Sans base_dn, on ne peut pas lire les attributs/groupes : identité
            # minimale (subject = DN de bind, groupes vides → mapping default).
            if not base_dn:
                return FederatedIdentity(subject=user_id, username=username,
                                         display_name=username, email="",
                                         groups=(), source="ldap")
            entry = self._search_user(conn, username)
            if entry is None:
                return FederatedIdentity(subject=user_id, username=username,
                                         display_name=username, email="",
                                         groups=(), source="ldap")
            groups = self._resolve_groups(conn, entry, entry.entry_dn)
            return self._build_identity(entry, entry.entry_dn, groups)
        finally:
            self._safe_unbind(conn)

    # ── recherche & attributs ────────────────────────────────────────────
    def _search_user(self, conn: Connection, username: str):
        base_dn = str(self._cfg.get("base_dn") or "").strip()
        template = str(self._cfg.get("user_filter") or "(&(objectClass=user)(sAMAccountName={username}))")
        flt = build_user_filter(template, username)
        attrs = [self._attr("id_attr", "objectGUID"), self._attr("username_attr", "sAMAccountName"),
                 self._attr("display_name_attr", "displayName"), self._attr("email_attr", "mail"),
                 "memberOf"]
        conn.search(base_dn, flt, search_scope=SUBTREE, attributes=attrs)
        entries = list(conn.entries)
        if len(entries) != 1:
            if len(entries) > 1:
                # Filtre ambigu = danger (mauvais compte lié) : on refuse plutôt que deviner.
                logger.warning("auth ldap : filtre ambigu pour '%s' (%d entrées) — refus", username, len(entries))
            return None
        return entries[0]

    def _resolve_groups(self, conn: Connection, entry, user_dn: str) -> tuple[str, ...]:
        if bool(self._cfg.get("resolve_nested_groups", False)):
            base_dn = str(self._cfg.get("base_dn") or "").strip()
            flt = f"(member:{_MATCHING_RULE_IN_CHAIN}:={escape_filter_chars(user_dn)})"
            conn.search(base_dn, flt, search_scope=SUBTREE, attributes=[])
            return tuple(e.entry_dn for e in conn.entries)
        return tuple(str(v) for v in self._attr_values(entry, "memberOf"))

    def _build_identity(self, entry, user_dn: str, groups: tuple[str, ...]) -> FederatedIdentity:
        # subject STABLE : objectGUID (survit aux renommages/déplacements AD) sinon
        # le DN normalisé — jamais le username ni l'email (§3.5).
        subject = self._stable_subject(entry) or user_dn.lower()
        username = self._first(entry, self._attr("username_attr", "sAMAccountName")) or user_dn
        display_name = self._first(entry, self._attr("display_name_attr", "displayName")) or username
        email = self._first(entry, self._attr("email_attr", "mail")) or ""
        return FederatedIdentity(subject=str(subject), username=str(username),
                                 display_name=str(display_name), email=str(email),
                                 groups=groups, source="ldap")

    def _stable_subject(self, entry) -> str | None:
        values = self._attr_values(entry, self._attr("id_attr", "objectGUID"))
        if not values:
            return None
        value = values[0]
        if isinstance(value, (bytes, bytearray)):
            return value.hex()          # objectGUID binaire → chaîne déterministe
        return str(value)

    # ── petits accès ldap3 tolérants ─────────────────────────────────────
    def _attr(self, key: str, default: str) -> str:
        return str(self._cfg.get(key) or default)

    @staticmethod
    def _attr_values(entry, name: str) -> list:
        try:
            attr = entry[name]
        except (KeyError, LDAPException):
            return []
        values = getattr(attr, "values", None)
        return list(values) if values else []

    def _first(self, entry, name: str) -> str:
        values = self._attr_values(entry, name)
        return str(values[0]) if values else ""

    @staticmethod
    def _safe_unbind(conn) -> None:
        try:
            if conn is not None:
                conn.unbind()
        except LDAPException:  # la déconnexion ne doit jamais masquer le résultat d'auth
            pass

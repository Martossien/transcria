"""Schéma de config — identité : backends local/oidc/proxy/ldap + fédération des rôles."""
from __future__ import annotations

from transcria.config.checks.base import (  # noqa: F401
    ValidationResult,
    _check_bool,
    _check_int_range,
    _check_optional_number,
    _check_optional_positive_int,
    _check_port_value,
    _check_regex_list,
    _check_regex_string,
    _check_str,
    _check_time_string,
)

_IMPLEMENTED_AUTH_BACKENDS = ("local", "oidc", "proxy", "ldap")  # GESTION_IDENTITE.md

def _check_auth(auth: dict, r: ValidationResult) -> None:
    _check_bool(auth, "enabled", "auth.enabled", r)
    _check_str(auth, "first_admin_username", "auth.first_admin_username", r)
    _check_str(auth, "first_admin_password", "auth.first_admin_password", r)
    pwd = auth.get("first_admin_password", "")
    if isinstance(pwd, str) and pwd in ("CHANGE-ME", "admin-change-me", ""):
        r.add_warning(
            "Sécurité : auth.first_admin_password utilise la valeur par défaut. "
            "Changez-la dès que possible."
        )

def _check_auth_backend(auth: dict, r: ValidationResult) -> None:
    backend = str(auth.get("backend", "local") or "local").strip().lower()
    if backend not in _IMPLEMENTED_AUTH_BACKENDS:
        r.add_error(
            f"auth.backend='{backend}' non disponible. Implémentés : "
            f"{', '.join(_IMPLEMENTED_AUTH_BACKENDS)} (cf. docs/GESTION_IDENTITE.md) — "
            f"jamais de repli silencieux vers 'local'."
        )

def _check_auth_oidc(auth: dict, r: ValidationResult) -> None:
    """Bloc auth.oidc + role_mapping : requis SEULEMENT si backend=oidc."""
    if str(auth.get("backend", "local") or "local").strip().lower() != "oidc":
        return
    oidc = auth.get("oidc") or {}
    if not str(oidc.get("issuer") or "").strip().startswith(("https://", "http://")):
        r.add_error("auth.oidc.issuer requis (URL https de l'IdP) quand auth.backend=oidc")
    if not str(oidc.get("client_id") or "").strip():
        r.add_error("auth.oidc.client_id requis quand auth.backend=oidc")
    if not (str(oidc.get("client_secret") or "").strip() or str(oidc.get("client_secret_env") or "").strip()):
        r.add_error("auth.oidc.client_secret (ou client_secret_env) requis quand auth.backend=oidc")
    issuer = str(oidc.get("issuer") or "")
    if issuer.startswith("http://") and "127.0.0.1" not in issuer and "localhost" not in issuer:
        r.add_warning("auth.oidc.issuer en HTTP non-loopback : les jetons transitent en clair")
    _check_role_mapping_federated(auth, r)

def _check_auth_proxy(auth: dict, r: ValidationResult) -> None:
    """Bloc auth.proxy : requis SEULEMENT si backend=proxy (GESTION_IDENTITE §3.7)."""
    if str(auth.get("backend", "local") or "local").strip().lower() != "proxy":
        return
    proxy = auth.get("proxy") or {}
    trusted = proxy.get("trusted_ips") or []
    if not trusted:
        r.add_error("auth.proxy.trusted_ips requis (adresses/CIDR du proxy frontal) quand "
                    "auth.backend=proxy — liste vide = personne n'est de confiance.")
    import ipaddress

    for entry in trusted:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except ValueError:
            r.add_error(f"auth.proxy.trusted_ips : entrée invalide '{entry}' (adresse IP ou CIDR attendu)")
            continue
        if net.num_addresses > 65536:
            r.add_warning(f"auth.proxy.trusted_ips : '{entry}' couvre un réseau très large — "
                          "n'importe quelle machine de ce réseau peut se faire passer pour "
                          "n'importe quel utilisateur.")
    if not str(proxy.get("user_header") or "").strip():
        r.add_error("auth.proxy.user_header ne peut pas être vide quand auth.backend=proxy")
    _check_role_mapping_federated(auth, r)

def _check_auth_ldap(auth: dict, r: ValidationResult) -> None:
    """Bloc auth.ldap : requis SEULEMENT si backend=ldap (GESTION_IDENTITE §3.4)."""
    if str(auth.get("backend", "local") or "local").strip().lower() != "ldap":
        return
    ldap = auth.get("ldap") or {}
    servers = ldap.get("servers") or []
    if isinstance(servers, str):
        servers = [servers]
    if not servers:
        r.add_error("auth.ldap.servers requis (au moins un contrôleur, ex. ldaps://dc1.corp) "
                    "quand auth.backend=ldap")
    # Canal chiffré OBLIGATOIRE : LDAPS ou StartTLS. En clair uniquement si
    # allow_plaintext est posé EXPLICITEMENT (jamais un mot de passe en clair par défaut).
    use_ssl = bool(ldap.get("use_ssl", True))
    start_tls = bool(ldap.get("start_tls", False))
    if not use_ssl and not start_tls and not bool(ldap.get("allow_plaintext", False)):
        r.add_error("auth.ldap : canal non chiffré. Activez use_ssl (LDAPS) ou start_tls, "
                    "ou posez auth.ldap.allow_plaintext: true en toute connaissance de cause "
                    "(le mot de passe transiterait en clair).")
    for uri in servers:
        if str(uri).strip().lower().startswith("ldaps://") and not use_ssl:
            r.add_warning(f"auth.ldap.servers : '{uri}' est en ldaps:// mais use_ssl=false — "
                          "activez use_ssl pour établir réellement TLS.")
    bind_mode = str(ldap.get("bind_mode") or "service").strip().lower()
    if bind_mode not in ("service", "direct"):
        r.add_error(f"auth.ldap.bind_mode='{bind_mode}' invalide (attendu : service | direct)")
    if bind_mode == "service":
        if not str(ldap.get("service_dn") or "").strip():
            r.add_error("auth.ldap.service_dn requis en bind_mode=service (compte de lecture de l'annuaire)")
        if not (str(ldap.get("service_password") or "").strip()
                or str(ldap.get("service_password_env") or "").strip()):
            r.add_error("auth.ldap.service_password (ou service_password_env) requis en bind_mode=service")
        if not str(ldap.get("base_dn") or "").strip():
            r.add_error("auth.ldap.base_dn requis en bind_mode=service (racine de recherche)")
        if "{username}" not in str(ldap.get("user_filter") or "{username}"):
            r.add_error("auth.ldap.user_filter doit contenir {username}")
    elif "{username}" not in str(ldap.get("user_dn_template") or ""):
        r.add_error("auth.ldap.user_dn_template requis avec {username} en bind_mode=direct")
    _check_role_mapping_federated(auth, r)

def _check_role_mapping_federated(auth: dict, r: ValidationResult) -> None:
    # Mapping : validé par le module PUR (mêmes règles pour tous les connecteurs).
    from transcria.auth.identity.mapping import validate_role_mapping

    for err in validate_role_mapping(auth.get("role_mapping") or {}):
        r.add_error(err)
    if not ((auth.get("role_mapping") or {}).get("rules")):
        r.add_warning("auth.role_mapping.rules vide : personne n'obtiendra mieux que le défaut "
                      f"('{(auth.get('role_mapping') or {}).get('default', 'deny')}')")

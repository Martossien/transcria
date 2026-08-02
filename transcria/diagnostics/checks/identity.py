"""Doctor — identité (local/OIDC/proxy/LDAP) et transport HTTP(S)."""
from __future__ import annotations

from collections.abc import Callable

from transcria.diagnostics.checks.common import FAIL, OK, WARN, CheckResult, _t


def check_identity_backend(
    cfg: dict,
    *,
    discovery_prober: Callable[[str], bool] | None = None,
    admin_counter: Callable[[], int] | None = None,
    ldap_prober: Callable[[str, int], bool] | None = None,
) -> CheckResult:
    """Chantier identité : backend fédéré actif → IdP joignable ET break-glass garanti.

    - découverte OIDC (`{issuer}/.well-known/openid-configuration`) sondée en HTTP ;
    - LDAP : chaque contrôleur sondé au niveau TCP (host:port ouvert) + rappel LDAPS ;
    - au moins UN admin LOCAL actif doit exister (sinon une panne d'IdP verrouille
      tout le monde dehors — FAIL, cf. GESTION_IDENTITE §3.9)."""
    name = _t("chk_identity")
    backend = str(((cfg.get("auth", {}) or {}).get("backend")) or "local").strip().lower()
    if backend == "local":
        return CheckResult(name, OK, _t("idn_local"))

    problems: list[str] = []
    if admin_counter is None:
        def admin_counter() -> int:
            from transcria.auth.models import Role, User

            return User.query.filter_by(role=Role.ADMIN.value, is_active=True,
                                        identity_source="local").count()
    try:
        local_admins = admin_counter()
    except Exception as exc:  # noqa: BLE001 — base indisponible = diagnostic impossible
        return CheckResult(name, WARN, _t("idn_db_unavailable", exc=exc))
    if local_admins == 0:
        return CheckResult(name, FAIL, _t("idn_no_local_admin", backend=backend),
                           hint=_t("idn_no_local_admin_hint"))

    if backend == "oidc":
        issuer = str(((cfg.get("auth", {}) or {}).get("oidc", {}) or {}).get("issuer") or "").rstrip("/")
        if discovery_prober is None:
            def discovery_prober(url: str) -> bool:
                import requests

                try:
                    return requests.get(url, timeout=5).status_code == 200
                except Exception:  # noqa: BLE001
                    return False
        url = f"{issuer}/.well-known/openid-configuration"
        if not issuer or not discovery_prober(url):
            problems.append(_t("idn_discovery_ko", url=url))
    if backend == "proxy":
        # GESTION_IDENTITE §3.7 : un réseau de confiance trop large = n'importe
        # quelle machine peut poser Remote-User et devenir n'importe qui.
        import ipaddress

        for entry in ((cfg.get("auth", {}) or {}).get("proxy", {}) or {}).get("trusted_ips") or []:
            try:
                net = ipaddress.ip_network(str(entry).strip(), strict=False)
            except ValueError:
                continue  # le schéma de config refuse déjà l'entrée invalide
            if net.num_addresses > 65536:
                problems.append(_t("idn_proxy_open_network", entry=entry))
    if backend == "ldap":
        problems.extend(_check_ldap_reachability(cfg, ldap_prober))
    if problems:
        return CheckResult(name, WARN, " ; ".join(problems), hint=_t("idn_discovery_hint"))
    return CheckResult(name, OK, _t("idn_ok", backend=backend, admins=local_admins))

def _check_ldap_reachability(cfg: dict, ldap_prober) -> list[str]:
    """Sonde TCP de chaque contrôleur LDAP + rappels de sécurité (LDAPS, plaintext).

    On ne tente PAS de bind ici (pas de secret dans le doctor) : on vérifie que
    l'hôte:port répond, et on signale un canal non chiffré autorisé — c'est le
    diagnostic le plus utile sans divulguer d'identifiants."""
    import socket as _socket
    from urllib.parse import urlparse

    ldap_cfg = ((cfg.get("auth", {}) or {}).get("ldap", {}) or {})
    problems: list[str] = []
    servers = ldap_cfg.get("servers") or []
    if isinstance(servers, str):
        servers = [servers]
    use_ssl = bool(ldap_cfg.get("use_ssl", True))
    if not use_ssl and not bool(ldap_cfg.get("start_tls", False)) and bool(ldap_cfg.get("allow_plaintext", False)):
        problems.append(_t("idn_ldap_plaintext"))

    if ldap_prober is None:
        def ldap_prober(host: str, port: int) -> bool:
            try:
                with _socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                return False

    for uri in servers:
        parsed = urlparse(str(uri) if "://" in str(uri) else f"ldap://{uri}")
        host = parsed.hostname or str(uri)
        port = parsed.port or (636 if (parsed.scheme == "ldaps" or use_ssl) else 389)
        if not ldap_prober(host, port):
            problems.append(_t("idn_ldap_unreachable", host=host, port=port))
    return problems

def check_transport_security(cfg: dict) -> CheckResult:
    """Posture HTTP(S) : un backend d'auth FÉDÉRÉ (mots de passe d'annuaire, jetons
    OIDC) sans cookie sécurisé ni proxy TLS déclaré = identifiants d'entreprise sur un
    transport potentiellement en clair. WARN ciblé (le HTTP reste légitime en dev/local)."""
    name = _t("chk_transport")
    sec = (cfg.get("security", {}) or {})
    backend = str(((cfg.get("auth", {}) or {}).get("backend")) or "local").strip().lower()
    secure = bool(sec.get("session_cookie_secure", False)) or bool(sec.get("behind_tls_proxy", False))
    if backend in ("oidc", "proxy", "ldap") and not secure:
        return CheckResult(name, WARN, _t("transport_federated_insecure", backend=backend),
                           hint=_t("transport_hint"))
    return CheckResult(name, OK, _t("transport_ok", secure="oui" if secure else "non (local/HTTP)"))

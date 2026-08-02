"""Doctor — identité (local/OIDC/proxy/LDAP) et transport HTTP(S)."""
from __future__ import annotations

import os
from collections.abc import Callable

from transcria.config.loader import get_config_path
from transcria.diagnostics.checks.common import FAIL, OK, WARN, CheckResult, _t
from transcria.web.connector_catalog import load_catalog


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

def _connecteurs_exposes(cfg: dict) -> list[str]:
    """Connecteurs CONFIGURÉS qui exigent un point d'entrée HTTPS **public**.

    On lit `path: webhook` dans le catalogue plutôt que de coder « zoom » et « teams » en
    dur : tout connecteur futur déclaré ainsi sera couvert sans qu'on y pense. Un
    connecteur dont les identifiants ne sont qu'à moitié saisis ne compte pas — des
    champs en cours de remplissage ne sont pas un déploiement exposé.

    `connector_catalog` vit sous `web/` pour des raisons historiques mais ne dépend ni de
    Flask ni du web : c'est un lecteur de données. Le dupliquer ici ferait deux lecteurs
    du même fichier, qui divergeraient.
    """
    meetings = ((cfg.get("connectors", {}) or {}).get("meetings", {}) or {})
    if not meetings.get("enabled", False):
        return []
    fournis = {k: str(v) for k, v in (meetings.get("platform_env") or {}).items() if str(v).strip()}
    if not fournis:
        return []
    try:
        catalogue = load_catalog()
    except Exception:  # noqa: BLE001 — catalogue illisible : on ne bloque pas le doctor
        return []
    exposes = []
    for connecteur in catalogue:
        if connecteur.path != "webhook" or not connecteur.requires:
            continue
        if all(champ.key in fournis for champ in connecteur.requires):
            exposes.append(connecteur.name)
    return exposes


def check_transport_security(cfg: dict) -> CheckResult:
    """Posture HTTP(S), rattachée à ce qui est réellement exposé.

    Deux niveaux, du plus grave au moins grave :

    - un **connecteur à webhook** (Zoom RTMS, Teams) configuré sans TLS → **FAIL**. Un
      point d'entrée public en clair transporte des jetons de plateforme : ce n'est pas
      une posture perfectible, c'est une erreur de déploiement (sécurité S2.1) ;
    - un **backend d'identité fédéré** sans cookie sécurisé ni proxy TLS → WARN ciblé
      (le HTTP reste légitime en dev/local).
    """
    name = _t("chk_transport")
    sec = (cfg.get("security", {}) or {})
    backend = str(((cfg.get("auth", {}) or {}).get("backend")) or "local").strip().lower()
    secure = bool(sec.get("session_cookie_secure", False)) or bool(sec.get("behind_tls_proxy", False))

    if not secure:
        exposes = _connecteurs_exposes(cfg)
        if exposes:
            return CheckResult(name, FAIL,
                               _t("transport_webhook_insecure", connecteurs=", ".join(exposes)),
                               hint=_t("transport_hint"))
        if backend in ("oidc", "proxy", "ldap"):
            return CheckResult(name, WARN, _t("transport_federated_insecure", backend=backend),
                               hint=_t("transport_hint"))
    return CheckResult(name, OK, _t("transport_ok", secure="oui" if secure else "non (local/HTTP)"))


def check_config_permissions(cfg: dict, *, config_path: str | None = None,
                             stat_fn: Callable[[str], int] | None = None) -> CheckResult:
    """Le fichier de configuration porte des SECRETS — il ne doit pas être lisible
    par les autres comptes de la machine.

    `save_config` pose désormais `0600` à chaque écriture, mais un fichier créé
    AVANT ce correctif (ou par un `cp` manuel, ou par une restauration de
    sauvegarde) garde ses permissions d'origine tant que personne n'enregistre
    depuis l'interface. Le contrôle regarde donc l'état réel sur disque, pas ce
    que le code aurait dû faire : c'est exactement l'écart qui a produit le
    `0644` constaté sur une installation réelle.

    WARN et non FAIL : le portail fonctionne parfaitement dans cet état — c'est
    une exposition, pas une panne, et un FAIL ferait échouer le doctor sur des
    installations par ailleurs saines.
    """
    name = _t("chk_config_perms")
    chemin = config_path or get_config_path()
    lire = stat_fn or (lambda p: os.stat(p).st_mode)
    try:
        mode = lire(chemin) & 0o777
    except OSError:
        # Pas de fichier = installation qui tourne sur les défauts : rien à protéger.
        return CheckResult(name, OK, _t("cfgperm_absent"))
    trop_large = mode & 0o077
    if trop_large:
        return CheckResult(name, WARN, _t("cfgperm_large", mode=f"{mode:04o}"),
                           hint=_t("cfgperm_hint", chemin=chemin))
    return CheckResult(name, OK, _t("cfgperm_ok", mode=f"{mode:04o}"))


def check_outbound_allowlist(cfg: dict) -> CheckResult:
    """Le connecteur Visio résout les salles en interrogeant l'hôte du LIEN de réunion.

    La garde sortante (S2.2) refuse ce qui n'est jamais une instance — la machine
    elle-même, les métadonnées cloud — mais elle ne peut pas deviner quels hôtes sont
    « chez vous » : un réseau local peut parfaitement être en adressage PUBLIC. Le seul
    mécanisme qui le sache est l'allowlist `VISIO_ALLOWED_HOSTS`.

    WARN et non FAIL : sans allowlist le service fonctionne, et l'exposition reste faible
    (aucune réponse n'est renvoyée à l'utilisateur). Mais un mécanisme facultatif que
    personne ne découvre ne protège personne — d'où ce rappel.
    """
    name = _t("chk_outbound")
    meetings = ((cfg.get("connectors", {}) or {}).get("meetings", {}) or {})
    penv = meetings.get("platform_env") or {}
    visio_actif = meetings.get("enabled", False) and any(
        str(k).startswith("VISIO_") and str(v).strip() for k, v in penv.items())
    if not visio_actif:
        return CheckResult(name, OK, _t("outbound_sans_objet"))
    if not (os.environ.get("VISIO_ALLOWED_HOSTS", "") or "").strip():
        return CheckResult(name, WARN, _t("outbound_sans_allowlist"),
                           hint=_t("outbound_hint"))
    return CheckResult(name, OK, _t("outbound_ok"))

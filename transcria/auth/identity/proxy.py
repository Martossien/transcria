"""Connecteur « proxy de confiance » — docs/GESTION_IDENTITE.md §3.7 (lot 3).

Le standard de facto du self-hosted (Authelia, oauth2-proxy, Pomerium) : le
proxy frontal authentifie l'utilisateur et transmet son identité dans des
en-têtes (`Remote-User`/`Remote-Groups`…). Toute la sécurité tient dans UNE
garde : ces en-têtes ne sont crus que si l'adresse SOCKET de l'appelant
(`request.remote_addr`, jamais `X-Forwarded-For` — falsifiable par
construction) figure dans `auth.proxy.trusted_ips`.

Le guide de déploiement (docs/INSTALL.md) impose la directive proxy qui ÉCRASE
les en-têtes entrants (`proxy_set_header Remote-User …` côté Nginx — jamais de
passthrough) : sans elle, un client enverrait lui-même l'en-tête à travers le
proxy déclaré.
"""
from __future__ import annotations

import ipaddress
import logging

from transcria.auth.identity.base import FederatedIdentity

logger = logging.getLogger(__name__)


def proxy_config(config: dict) -> dict:
    return ((config.get("auth", {}) or {}).get("proxy", {}) or {})


def is_trusted_addr(remote_addr: str | None, cfg: dict) -> bool:
    """L'adresse socket appartient-elle aux réseaux de confiance ?

    `trusted_ips` accepte des adresses (`10.0.0.5`) et des réseaux CIDR
    (`10.0.0.0/24`). Liste vide = personne n'est de confiance (le schéma de
    config refuse d'ailleurs le backend au boot dans ce cas).
    """
    if not remote_addr:
        return False
    try:
        addr = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    for entry in cfg.get("trusted_ips") or []:
        try:
            if addr in ipaddress.ip_network(str(entry).strip(), strict=False):
                return True
        except ValueError:
            logger.warning("auth.proxy.trusted_ips : entrée invalide ignorée — %r", entry)
    return False


def extract_identity(headers, remote_addr: str | None, config: dict) -> FederatedIdentity | None:
    """Lit l'identité des en-têtes du proxy — ou None si rien de crédible.

    Garde non négociable (§3.7) : des en-têtes d'identité portés par une
    requête dont l'adresse socket n'est PAS le proxy déclaré sont une tentative
    d'usurpation → WARNING journalisé (avec l'IP) et en-têtes IGNORÉS.
    """
    cfg = proxy_config(config)
    user_header = str(cfg.get("user_header") or "Remote-User")
    username = str(headers.get(user_header) or "").strip()

    if not is_trusted_addr(remote_addr, cfg):
        if username:
            logger.warning(
                "auth proxy : en-tête %s reçu depuis une adresse NON déclarée %s — "
                "usurpation possible, en-têtes ignorés.", user_header, remote_addr,
            )
        return None
    if not username:
        return None

    groups_raw = str(headers.get(str(cfg.get("groups_header") or "Remote-Groups")) or "")
    display_name = str(headers.get(str(cfg.get("name_header") or "Remote-Name")) or "").strip()
    email = str(headers.get(str(cfg.get("email_header") or "Remote-Email")) or "").strip()
    return FederatedIdentity(
        # Pas de `sub` chez un proxy : le username EST l'identifiant stable —
        # l'appariement JIT se fait sur (source="proxy", subject=username).
        subject=username,
        username=username,
        display_name=display_name or username,
        email=email,
        groups=tuple(g.strip() for g in groups_raw.split(",") if g.strip()),
        source="proxy",
    )

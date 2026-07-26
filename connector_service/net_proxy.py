"""Respect de `no_proxy` pour les clients qui l'ignorent.

Problème RÉEL rencontré : derrière un proxy d'entreprise, le client LiveKit tente de joindre
même `127.0.0.1` **via le proxy** — qui refuse (`403 Forbidden`) — alors que la variable
`no_proxy` couvre explicitement la boucle locale. Le client honore `http_proxy` mais pas
`no_proxy` : la connexion échoue avec un message qui n'oriente pas vers la vraie cause.

Ce module rétablit la règle attendue : si l'hôte visé est couvert par `no_proxy`, on retire
les variables de proxy de l'environnement du PROCESSUS avant d'ouvrir la connexion. C'est
volontairement limité à ce cas — on ne désactive JAMAIS le proxy pour un hôte distant, qui en
a légitimement besoin.

La correspondance d'hôte est PURE et testable ; l'effet de bord sur l'environnement est isolé
dans une seule fonction.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import MutableMapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Variantes minuscules et majuscules : les deux ont cours selon les outils.
PROXY_ENV_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                  "all_proxy", "ALL_PROXY")
_NO_PROXY_VARS = ("no_proxy", "NO_PROXY")


def _host_of(url: str) -> str:
    """Hôte d'une URL, sans port. Tolère les schémas WebSocket."""
    parsed = urlsplit(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").strip().lower()


def _matches_rule(host: str, rule: str) -> bool:
    """Un hôte correspond-il à une entrée de `no_proxy` ?

    Entrées reconnues : `*` (tout), une adresse ou un réseau (`10.0.0.0/8`), un nom exact,
    ou un suffixe de domaine (`exemple.fr` couvre `visio.exemple.fr`).
    """
    rule = rule.strip().lower().lstrip(".")
    if not rule:
        return False
    if rule == "*":
        return True
    if "/" in rule:                                   # réseau CIDR
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(rule, strict=False)
        except ValueError:
            return False
    return host == rule or host.endswith("." + rule)


def host_bypasses_proxy(host: str, no_proxy: str) -> bool:
    """L'hôte est-il exclu du proxy par la règle `no_proxy` fournie ?"""
    if not host:
        return False
    return any(_matches_rule(host, rule) for rule in (no_proxy or "").split(","))


def clear_proxy_env_if_bypassed(url: str, env: MutableMapping[str, str] | None = None) -> bool:
    """Retire les variables de proxy du processus si l'URL vise un hôte exclu.

    Retourne True si un contournement a été appliqué. Sans effet quand aucun proxy n'est
    configuré, ou quand l'hôte doit légitimement passer par le proxy.
    """
    environment = os.environ if env is None else env
    if not any(environment.get(var) for var in PROXY_ENV_VARS):
        return False
    no_proxy = next((environment.get(var) or "" for var in _NO_PROXY_VARS
                     if environment.get(var)), "")
    host = _host_of(url)
    if not host_bypasses_proxy(host, no_proxy):
        return False
    for var in PROXY_ENV_VARS:
        environment.pop(var, None)
    logger.info("Proxy contourné pour %s (couvert par no_proxy) — le client ne gère pas "
                "no_proxy lui-même", host)
    return True

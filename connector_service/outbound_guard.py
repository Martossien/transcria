"""Garde des requêtes SORTANTES pilotées par une valeur d'utilisateur — sécurité S2.2.

Le bot Visio interroge l'API de l'hôte lu dans le **lien de réunion** fourni par un
utilisateur. Sans garde, celui-ci choisit donc l'hôte que le service contacte : SSRF
aveugle. Le repli est honnête (on retombe sur le slug de la salle), donc l'attaquant
apprend peu — mais la requête part, potentiellement vers un réseau interne.

**La difficulté de ce correctif tient au produit, pas à la technique.** La recette
habituelle anti-SSRF — refuser toutes les adresses privées — est ici *fausse* :
TranscrIA est auto-hébergé, et l'instance Visio d'un exploitant vit très probablement
sur son LAN (`192.168.x`, `10.x`). L'appliquer casserait le cas le plus courant, et la
garde serait retirée dans la semaine.

D'où deux niveaux :

1. **Toujours refusé** — ce qui n'est *jamais* une instance de visioconférence :
   la boucle locale (`127.0.0.0/8`, `::1`, `localhost`), l'adresse « toutes interfaces »
   et le lien-local (`169.254.0.0/16`, `fe80::/10`), qui porte les **métadonnées cloud**.
   Ce sont les deux pivots réels : atteindre un service qui n'écoute que sur la machine,
   ou lire des identifiants d'instance.
2. **Allowlist stricte** (`VISIO_ALLOWED_HOSTS`), quand l'exploitant la pose : seuls ces
   hôtes sont joignables. Elle ne peut pas rouvrir le niveau 1 — déclarer `localhost` par
   mégarde ne redonne pas le pivot. Pour viser délibérément sa propre machine, il y a
   `VISIO_API_BASE`, qui est une valeur d'exploitant et non un lien d'utilisateur.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

#: Noms d'hôte qui désignent la machine elle-même, quelle que soit la résolution.
_NOMS_LOCAUX = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

CLE_ALLOWLIST = "VISIO_ALLOWED_HOSTS"


class HoteRefuse(RuntimeError):
    """Hôte sortant refusé — le message porte le motif."""


def allowlist_depuis_environnement() -> list[str]:
    """Hôtes autorisés, lus dans ``VISIO_ALLOWED_HOSTS`` (séparés par des virgules)."""
    brut = os.environ.get(CLE_ALLOWLIST, "")
    return [h.strip().lower() for h in brut.split(",") if h.strip()]


def _est_toujours_interdit(hote: str) -> bool:
    """Boucle locale, « toutes interfaces », lien-local (métadonnées cloud)."""
    if hote.lower() in _NOMS_LOCAUX:
        return True
    try:
        adresse = ipaddress.ip_address(hote)
    except ValueError:
        return False   # un nom de domaine : on ne résout PAS (pas de DNS ici, cf. module)
    return bool(
        adresse.is_loopback
        or adresse.is_link_local        # 169.254.0.0/16 et fe80::/10
        or adresse.is_unspecified       # 0.0.0.0 / ::
    )


def verifier_hote_sortant(url: str, *, allowlist: list[str] | None = None) -> bool:
    """Vrai si la requête peut partir. Lève ``HoteRefuse`` sinon.

    `allowlist` non fournie → lue dans l'environnement.
    """
    liste = allowlist if allowlist is not None else allowlist_depuis_environnement()

    brut = (url or "").strip()
    if not brut:
        raise HoteRefuse("URL sortante vide")
    parts = urlsplit(brut)
    if parts.scheme not in ("http", "https"):
        raise HoteRefuse(f"schéma non autorisé pour une requête sortante : {parts.scheme or '(aucun)'}")
    if parts.username or parts.password:
        raise HoteRefuse("identifiants dans l'URL (userinfo) — l'hôte réel n'est pas celui qu'on lit")
    hote = parts.hostname
    if not hote:
        raise HoteRefuse("URL sortante sans nom d'hôte")

    # Niveau 1 : jamais joignable, même déclaré dans l'allowlist.
    if _est_toujours_interdit(hote):
        raise HoteRefuse(
            f"hôte interdit pour une requête pilotée par un lien d'utilisateur : {hote} "
            f"(boucle locale, « toutes interfaces » ou lien-local/métadonnées)"
        )

    # Niveau 2 : allowlist, si l'exploitant en a posé une.
    if liste and hote.lower() not in liste:
        raise HoteRefuse(
            f"hôte hors allowlist : {hote} (voir {CLE_ALLOWLIST})"
        )
    return True

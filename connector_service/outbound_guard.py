"""Garde des requêtes SORTANTES pilotées par une valeur d'utilisateur — sécurité S2.2.

Le bot Visio interroge l'API de l'hôte lu dans le **lien de réunion** fourni par un
utilisateur. Sans garde, celui-ci choisit donc l'hôte que le service contacte : SSRF
aveugle. Le repli est honnête (on retombe sur le slug de la salle), donc l'attaquant
apprend peu — mais la requête part, potentiellement vers un réseau interne.

**La difficulté de ce correctif tient au produit, pas à la technique.** La recette
habituelle anti-SSRF — refuser toutes les adresses privées — est ici *fausse*, pour deux
raisons qui se cumulent :

- TranscrIA est auto-hébergé : l'instance Visio d'un exploitant vit sur SON réseau ;
- et **un réseau local n'est pas forcément en adressage privé**. Une organisation qui
  dispose d'un bloc d'adresses publiques s'en sert en interne (réservation de plage pour
  simplifier le routage). Son instance est alors sur une IP *publique* et sur son LAN.

Autrement dit : **l'adresse ne dit pas si l'on est « chez soi »**. Une garde bâtie sur
« privé = interne » refuserait des déploiements légitimes tout en manquant son objet. Le
niveau 1 ci-dessous ne parle donc pas de plages : il refuse ce qui n'est *jamais* une
instance de visioconférence. La sécurité du reste, c'est l'allowlist — le seul mécanisme
qui sache distinguer « mon réseau » d'Internet quand l'adressage ne le dit pas.

D'où deux niveaux :

1. **Toujours refusé** — ce qui n'est *jamais* une instance de visioconférence :
   la boucle locale (`127.0.0.0/8`, `::1`, `localhost`), l'adresse « toutes interfaces »
   et le lien-local (`169.254.0.0/16`, `fe80::/10`), qui porte les **métadonnées cloud**.
   Ce sont les deux pivots réels : atteindre un service qui n'écoute que sur la machine,
   ou lire des identifiants d'instance. **Aucune plage privée ou publique n'est bornée
   ici** — voir plus haut pourquoi.
2. **Allowlist stricte** (`VISIO_ALLOWED_HOSTS`), quand l'exploitant la pose : seuls ces
   hôtes sont joignables. Elle ne peut pas rouvrir le niveau 1 — déclarer `localhost` par
   mégarde ne redonne pas le pivot. Pour viser délibérément sa propre machine, il y a
   `VISIO_API_BASE`, qui est une valeur d'exploitant et non un lien d'utilisateur.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
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


def _adresse_interdite(adresse: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Boucle locale, « toutes interfaces », lien-local (métadonnées cloud)."""
    return bool(adresse.is_loopback or adresse.is_link_local or adresse.is_unspecified)


def _resoudre(hote: str) -> list[str]:
    """Toutes les adresses derrière un nom. Liste vide si la résolution échoue.

    Injectable dans les tests : la garde doit être vérifiable sans DNS.
    """
    try:
        infos = socket.getaddrinfo(hote, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _est_toujours_interdit(hote: str) -> tuple[bool, str]:
    """Décide sur la DESTINATION, pas sur la forme écrite.

    Première version : on comparait le texte de l'hôte. Un second audit l'a démontée en
    cinq lignes — `2130706433`, `0x7f000001`, `017700000001` et `127.1` désignent tous
    `127.0.0.1` pour le résolveur système, mais `ipaddress` les rejette, donc ils passaient
    pour des noms de domaine. Et un nom qui RÉSOUT vers la boucle locale passait tout
    court : un attaquant contrôle son propre DNS.

    On résout donc, et **une seule** adresse interdite suffit à refuser — un nom
    multi-adresses ne doit pas passer parce que la première est saine.

    Limite assumée : entre cette vérification et la requête, le DNS peut changer (*DNS
    rebinding*). La fermer demanderait d'épingler l'adresse jusqu'à la connexion, ce qui
    n'est pas proportionné ici — le repli de l'appelant est le nom de la salle, et aucune
    réponse n'est renvoyée à l'utilisateur.
    """
    if hote.lower() in _NOMS_LOCAUX:
        return True, hote
    adresses = _resoudre(hote)
    if not adresses:
        return True, "aucune adresse (résolution impossible)"
    for brute in adresses:
        try:
            adresse = ipaddress.ip_address(brute)
        except ValueError:
            return True, f"adresse illisible ({brute})"
        if _adresse_interdite(adresse):
            return True, brute
    return False, ""


def ouvreur_sans_redirection() -> urllib.request.OpenerDirector:
    """Ouvreur HTTP qui NE SUIT PAS les redirections.

    `urlopen` les suit par défaut : un hôte parfaitement légitime qui répond `302 Location:
    http://127.0.0.1/` contournait toute vérification faite en amont — on validait la
    première URL et la bibliothèque allait ailleurs. Refuser la redirection est ici sans
    coût : l'API interrogée n'en émet pas dans son fonctionnement normal.
    """
    class _SansRedirection(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    return urllib.request.build_opener(_SansRedirection)


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
    interdit, motif = _est_toujours_interdit(hote)
    if interdit:
        raise HoteRefuse(
            f"hôte interdit pour une requête pilotée par un lien d'utilisateur : {hote} "
            f"→ {motif} (boucle locale, « toutes interfaces » ou lien-local/métadonnées)"
        )

    # Niveau 2 : allowlist, si l'exploitant en a posé une.
    if liste and hote.lower() not in liste:
        raise HoteRefuse(
            f"hôte hors allowlist : {hote} (voir {CLE_ALLOWLIST})"
        )
    return True

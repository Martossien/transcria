"""Signature et vérification RS256 — la moitié cryptographique de nos deux connecteurs cloud.

Deux besoins symétriques, que `oauth_tokens.py` et `graph_validation.py` laissaient ouverts :

- **Google** : l'assertion d'un compte de service doit être SIGNÉE en RS256 avec la clé privée
  du fichier JSON. `oauth_tokens.google_assertion_claims` en produit les revendications ; il
  manquait de quoi les signer.
- **Microsoft** : les `validationTokens` d'une notification Graph doivent être VÉRIFIÉS contre
  les clés publiques de la plateforme d'identité. `graph_validation.check_claims` examine les
  revendications d'un jeton *déjà* vérifié ; il manquait la vérification elle-même.

POURQUOI PyJWT ET NON DU CODE MAISON. `signatures.py` calcule bien un JWT HS256 à la main pour
Zoom, et c'est sans danger : **signer** n'offre aucune prise à un attaquant. **Vérifier**, si.
Les deux pièges classiques — accepter `alg: none`, ou accepter un HS256 dont la clé serait la
clé PUBLIQUE RSA — transforment la vérification en simple décoration. On délègue donc à une
bibliothèque éprouvée, en lui imposant une liste blanche d'algorithmes, et on teste les deux
attaques pour que la protection reste vraie après une mise à jour.

CE QUI RESTE HORS D'ICI : tout appel réseau. Le JWKS est PASSÉ en argument ; c'est ce qui rend
ce module vérifiable en CI, avec une paire de clés engendrée sur place.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError

# Seul algorithme accepté, à la signature comme à la vérification. Google impose RS256 pour les
# assertions de compte de service, et Microsoft signe ses `validationTokens` en RS256.
RS256 = "RS256"
ALLOWED_ALGORITHMS = (RS256,)

# Tolérance d'horloge à la vérification. Sans elle, une dérive de quelques secondes entre notre
# machine et Microsoft ferait rejeter des notifications parfaitement authentiques.
CLOCK_SKEW_SECONDS = 300


class SigningKeyError(ValueError):
    """Clé de signature ou de vérification inutilisable."""


class VerificationError(ValueError):
    """Jeton refusé. Le message dit POURQUOI : sans lui, le diagnostic est une devinette."""


@dataclass(frozen=True)
class ServiceAccountKey:
    """Ce qu'un fichier de clé de compte de service Google apporte d'utile."""

    client_email: str
    private_key: str
    private_key_id: str = ""
    token_uri: str = ""


def load_service_account(raw: Any) -> ServiceAccountKey:
    """Fichier JSON de compte de service → clé exploitable. PURE, donc testée.

    Les contrôles ne sont pas de la coquetterie : le fichier téléchargé depuis la console
    Google ressemble beaucoup à d'AUTRES fichiers d'identifiants (client OAuth « installé »,
    par exemple), et les confondre produit une erreur d'authentification opaque, très loin de
    sa cause. On refuse donc ici, en nommant ce qui manque.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SigningKeyError("clé de compte de service illisible (JSON attendu)") from exc
    if not isinstance(raw, dict):
        raise SigningKeyError("clé de compte de service inexploitable")

    type_declare = str(raw.get("type") or "")
    if type_declare and type_declare != "service_account":
        raise SigningKeyError(
            f"ce fichier est de type « {type_declare} » et non « service_account » — "
            f"un identifiant OAuth client ne convient pas ici")

    email = str(raw.get("client_email") or "")
    private_key = str(raw.get("private_key") or "")
    if not email:
        raise SigningKeyError("« client_email » absent de la clé de compte de service")
    if not private_key:
        raise SigningKeyError("« private_key » absente de la clé de compte de service")
    if "PRIVATE KEY" not in private_key:
        raise SigningKeyError("« private_key » ne ressemble pas à une clé PEM")

    return ServiceAccountKey(
        client_email=email,
        private_key=private_key,
        private_key_id=str(raw.get("private_key_id") or ""),
        token_uri=str(raw.get("token_uri") or ""),
    )


def sign_assertion(claims: dict[str, Any], key: ServiceAccountKey) -> str:
    """Revendications + clé privée → assertion JWT signée en RS256.

    L'identifiant de clé voyage dans l'en-tête (`kid`) quand il est connu : Google s'en sert
    pour retrouver la bonne clé publique après une rotation, et son absence transformerait une
    rotation banale en panne d'authentification.
    """
    if not claims:
        raise SigningKeyError("aucune revendication à signer")
    headers = {"kid": key.private_key_id} if key.private_key_id else None
    try:
        return jwt.encode(claims, key.private_key, algorithm=RS256, headers=headers)
    except Exception as exc:  # noqa: BLE001 — clé malformée : on veut une erreur nommée
        raise SigningKeyError(f"signature impossible : {exc}") from exc


def select_signing_key(jwks: Any, key_id: str) -> Any:
    """Document JWKS + `kid` → clé publique prête à vérifier. PURE.

    Les clés de Microsoft TOURNENT quotidiennement et le document en publie plusieurs à la
    fois. Choisir « la première » marcherait la plupart du temps et échouerait un jour sur
    deux, au hasard du renouvellement : on exige donc le `kid`, et on refuse explicitement
    quand il est introuvable — c'est le signal qu'il faut rafraîchir le document.
    """
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise SigningKeyError("document JWKS invalide : clé « keys » (liste) attendue")
    if not key_id:
        raise SigningKeyError("jeton sans « kid » : la clé de signature ne peut être identifiée")
    for entree in jwks["keys"]:
        if isinstance(entree, dict) and entree.get("kid") == key_id:
            try:
                return jwt.PyJWK.from_dict(entree).key
            except Exception as exc:  # noqa: BLE001
                raise SigningKeyError(f"clé JWKS « {key_id} » inexploitable : {exc}") from exc
    raise SigningKeyError(
        f"aucune clé « {key_id} » dans le JWKS — document probablement périmé, "
        f"les clés de signature tournent quotidiennement")


def unverified_key_id(token: str) -> str:
    """`kid` annoncé par l'en-tête, AVANT toute vérification.

    Lire un en-tête non vérifié n'est pas une entorse : il faut bien savoir QUELLE clé
    demander pour pouvoir vérifier. Rien de ce qu'il contient n'est cru ensuite — la clé
    choisie ne vaut que si la signature tient.
    """
    try:
        return str(jwt.get_unverified_header(token).get("kid") or "")
    except InvalidTokenError as exc:
        raise VerificationError(f"en-tête de jeton illisible : {exc}") from exc


def verify_token(token: str, public_key: Any, *, audiences: set[str],
                 issuer: str = "", leeway: int = CLOCK_SKEW_SECONDS) -> dict[str, Any]:
    """Vérifie signature, échéance et audience, puis rend les revendications.

    `algorithms` est une LISTE BLANCHE, et c'est le point qui compte : sans elle, un jeton
    forgé avec `alg: none` ou signé en HS256 avec la clé publique RSA serait accepté. Les deux
    attaques sont testées ; la protection doit rester vraie après une mise à jour de PyJWT.

    L'audience est OBLIGATOIRE : un jeton authentique mais destiné à une autre application
    n'est pas le nôtre. Les revendications propres à Graph (`appid`/`azp`) sont examinées
    ensuite par `graph_validation.check_claims` — la séparation permet de tester cette
    logique-là sans aucune cryptographie.
    """
    if not audiences:
        raise VerificationError(
            "aucune audience attendue : accepter n'importe quelle audience reviendrait à "
            "accepter un jeton destiné à une autre application")
    # `Any` assumé : le TypedDict d'options de PyJWT ne décrit pas « require ».
    options: Any = {"require": ["exp", "aud"]}
    try:
        return jwt.decode(
            token,
            public_key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=list(audiences),
            issuer=issuer or None,
            leeway=leeway,
            options=options,
        )
    except InvalidTokenError as exc:
        raise VerificationError(f"jeton refusé : {type(exc).__name__} — {exc}") from exc

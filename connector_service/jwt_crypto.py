"""Vérification RS256 des jetons Microsoft Graph — la pièce que rien d'autre ne couvrait.

Les `validationTokens` d'une notification Graph sont des JWT signés par la plateforme
d'identité Microsoft, et la documentation demande de les vérifier TOUS.
`graph_validation.check_claims` examine les revendications d'un jeton *déjà* vérifié ; il
manquait la vérification elle-même.

CE MODULE NE SIGNE RIEN. La signature des assertions Google est le travail de
`connector_service/oauth.py`, qui la délègue à `google-auth` — dépendance déjà déclarée. Une
première version de ce module la réimplémentait : c'était refaire, moins bien, ce qui existait.

POURQUOI PyJWT ET NON DU CODE MAISON. `signatures.py` calcule bien un JWT HS256 à la main pour
Zoom, et c'est sans danger : **signer** n'offre aucune prise à un attaquant. **Vérifier**, si.
Les deux pièges classiques — accepter `alg: none`, ou accepter un HS256 dont la clé serait la
clé PUBLIQUE RSA — transforment la vérification en simple décoration. On délègue donc à une
bibliothèque éprouvée, en lui imposant une liste blanche d'algorithmes, et on teste les deux
attaques pour que la protection reste vraie après une mise à jour.

CE QUI RESTE HORS D'ICI : tout appel réseau. Le JWKS est PASSÉ en argument ; c'est ce qui rend
ce module vérifiable en CI, avec une paire de clés engendrée sur place.

⚠ PyJWT est une dépendance OPT-IN (`requirements-connectors.txt`) : elle est donc importée
PARESSEUSEMENT, comme `cryptography` dans `signatures.py`. L'importer en tête ferait échouer
l'import de `connector_service` sur une installation sans connecteurs — et, vécu, la COLLECTE
de toute la suite de tests en CI.
"""
from __future__ import annotations

from typing import Any

# Seul algorithme accepté. Microsoft signe ses `validationTokens` en RS256.
RS256 = "RS256"
ALLOWED_ALGORITHMS = (RS256,)

# Tolérance d'horloge à la vérification. Sans elle, une dérive de quelques secondes entre notre
# machine et Microsoft ferait rejeter des notifications parfaitement authentiques.
CLOCK_SKEW_SECONDS = 300


class SigningKeyError(ValueError):
    """Clé de vérification inutilisable."""


def _jwt() -> Any:
    """Charge PyJWT à la DEMANDE — même convention que `cryptography` dans `signatures.py`.

    PyJWT est une dépendance OPT-IN (`requirements-connectors.txt`) : l'importer au niveau du
    module ferait échouer l'import de `connector_service` sur une installation qui n'active pas
    les connecteurs, et — vécu — la COLLECTE de la suite de tests.
    """
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise SigningKeyError(
            "PyJWT absent : installer « pip install -r requirements-connectors.txt » "
            "pour vérifier les notifications Microsoft Graph") from exc
    return jwt


class VerificationError(ValueError):
    """Jeton refusé. Le message dit POURQUOI : sans lui, le diagnostic est une devinette."""


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
    jwt = _jwt()
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
    jwt = _jwt()
    try:
        return str(jwt.get_unverified_header(token).get("kid") or "")
    except jwt.InvalidTokenError as exc:
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
    jwt = _jwt()
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
    except jwt.InvalidTokenError as exc:
        raise VerificationError(f"jeton refusé : {type(exc).__name__} — {exc}") from exc

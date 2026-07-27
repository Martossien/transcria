"""Authenticité des notifications Microsoft Graph — décisions PURES, donc testables.

Notre receveur Teams ne vérifiait que le `clientState`, ce qui est la barrière la plus faible :
n'importe qui connaissant l'URL peut en poster un. Graph joint aux notifications riches des
`validationTokens` — des JWT signés par la plateforme d'identité Microsoft — et la
documentation demande de les vérifier TOUS.

Ce module ne contient QUE la logique de décision sur les revendications, pour qu'elle soit
vérifiable en CI sans réseau ni locataire. La résolution des clés de signature (JWKS) et la
vérification cryptographique vivent dans la couche réseau, qui les lui passe.

⚠ RÈGLE DE RÉPONSE, contre-intuitive mais explicite dans la documentation : répondre
`202 Accepted` IMMÉDIATEMENT, **avant** toute validation, même si celle-ci échoue ensuite.
Répondre autrement (401, 403) renseigne un attaquant sur ce qui a été détecté et provoque des
réémissions inutiles. On accepte, puis on ignore en silence ce qui ne se valide pas.
"""
from __future__ import annotations

from dataclasses import dataclass

# Identité de l'émetteur des notifications : le service « Microsoft Graph Change Tracking ».
# C'est LA valeur qui distingue une notification authentique d'une notification signée par
# une autre application Microsoft — la documentation avertit : « Failure to validate the
# appropriate claim may result in accepting notifications from an untrusted publisher ».
GRAPH_CHANGE_TRACKING_APP_ID = "0bf30f3b-4a52-48df-9a82-234910c4a086"

# Où trouver les clés de signature. Elles tournent quotidiennement : la couche réseau doit
# les rafraîchir, pas les figer.
OPENID_CONFIGURATION = "https://login.microsoftonline.com/common/.well-known/openid-configuration"


@dataclass(frozen=True)
class TokenVerdict:
    """Résultat de l'examen d'un jeton — et POURQUOI, pour que le journal soit utile."""

    valid: bool
    reason: str = ""


def check_claims(claims: dict, *, expected_audiences: set[str],
                 expected_tenant_id: str = "") -> TokenVerdict:
    """Examine les revendications d'un `validationToken` déjà vérifié cryptographiquement.

    La signature, l'expiration et l'émetteur relèvent de la bibliothèque JWT ; ce qu'elle ne
    fait PAS, et que la documentation impose, c'est de vérifier l'identité de l'APPELANT :

    - jeton v1.0 (`ver` = « 1.0 ») → la revendication `appid` ;
    - jeton v2.0 (`ver` = « 2.0 ») → la revendication `azp`.

    Se tromper de revendication revient à accepter des notifications d'un émetteur
    quelconque — c'est le point que la documentation souligne en gras.
    """
    audience = str(claims.get("aud") or "")
    if not audience:
        return TokenVerdict(False, "revendication « aud » absente")
    if audience not in expected_audiences:
        return TokenVerdict(False, f"audience {audience!r} inattendue — le jeton ne nous "
                                   f"est pas destiné")

    version = str(claims.get("ver") or "")
    caller_claim = "azp" if version.startswith("2") else "appid"
    caller = str(claims.get(caller_claim) or "")
    if not caller:
        return TokenVerdict(False, f"revendication « {caller_claim} » absente pour un jeton "
                                   f"v{version or '?'} — émetteur non identifiable")
    if caller != GRAPH_CHANGE_TRACKING_APP_ID:
        return TokenVerdict(False, f"émetteur {caller!r} : ce n'est pas le service de "
                                   f"notifications de Graph")

    if expected_tenant_id:
        tenant = str(claims.get("tid") or "")
        if tenant != expected_tenant_id:
            return TokenVerdict(False, f"locataire {tenant!r} inattendu")

    return TokenVerdict(True)


def all_tokens_valid(verdicts: list[TokenVerdict]) -> bool:
    """Tous les jetons du lot sont-ils valides ?

    La documentation demande de valider CHAQUE jeton : un lot peut mêler des éléments
    destinés à plusieurs applications ou locataires. Un seul échec rend l'ensemble suspect —
    on n'en retient donc aucun. Un lot VIDE n'est pas valide non plus : une notification
    riche sans jeton signale une configuration incorrecte de l'application côté Graph, cas
    que la documentation décrit explicitement (jeton `null`).
    """
    return bool(verdicts) and all(v.valid for v in verdicts)


def extract_validation_tokens(payload: object) -> list[str]:
    """Jetons présents dans la charge utile. Tolérant : une charge illisible rend une liste
    vide, ce qui fera échouer la validation — jamais lever."""
    if not isinstance(payload, dict):
        return []
    tokens = payload.get("validationTokens")
    if not isinstance(tokens, list):
        return []
    return [t for t in tokens if isinstance(t, str) and t]

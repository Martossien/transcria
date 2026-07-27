"""Jetons d'accès Microsoft Graph et Google — parties PURES, brique commune.

Les deux plateformes demandent la même chose : obtenir un jeton, le garder, le renouveler
AVANT qu'il n'expire. Seule la façon de le demander diffère.

- **Graph** : flux « client credentials » — un simple formulaire vers
  `login.microsoftonline.com/{locataire}/oauth2/v2.0/token`, portée `.default`.
- **Google** : le service account signe une ASSERTION JWT (RS256) qu'il échange contre un
  jeton. Pour lire les artefacts d'un utilisateur, la revendication `sub` désigne la personne
  à représenter — c'est la « délégation à l'échelle du domaine », que l'admin autorise.

CE QUI EST PUR ICI : la construction des demandes, la lecture des réponses, et la décision de
rafraîchissement. La SIGNATURE de l'assertion Google (clé privée RSA) et les appels réseau
vivent ailleurs — c'est ce qui rend cette logique vérifiable sans compte ni secret.

⚠ Le rafraîchissement anticipé n'est pas une élégance : un jeton qui expire pendant un
téléchargement d'enregistrement fait échouer l'ingestion à mi-course, et Graph émet en outre
des demandes de réautorisation quand le jeton approche de sa fin.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Marge de rafraîchissement. Les jetons vivent typiquement une heure ; cinq minutes couvrent
# un téléchargement d'enregistrement en cours sans multiplier les demandes.
REFRESH_MARGIN = timedelta(minutes=5)

# Durée de vie maximale d'une assertion Google, imposée par Google.
ASSERTION_MAX_LIFETIME = timedelta(hours=1)


class TokenError(ValueError):
    """Demande de jeton incohérente, ou réponse inexploitable."""


def graph_token_request(*, tenant_id: str, client_id: str, client_secret: str,
                        scope: str = GRAPH_DEFAULT_SCOPE) -> tuple[str, dict[str, str]]:
    """(URL, formulaire) pour un jeton applicatif Graph. PURE.

    La portée est `.default` et non une liste explicite : en flux « client credentials », ce
    sont les permissions CONSENTIES par l'administrateur qui font foi, pas ce que le code
    demande. Réclamer une portée nommée ici échouerait sans rien apprendre d'utile.
    """
    for nom, valeur in (("tenant_id", tenant_id), ("client_id", client_id),
                        ("client_secret", client_secret)):
        if not valeur:
            raise TokenError(f"{nom} requis pour demander un jeton Graph")
    return GRAPH_TOKEN_URL.format(tenant=tenant_id), {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }


def google_assertion_claims(*, service_account_email: str, scopes: tuple[str, ...],
                            now: datetime, subject: str = "",
                            lifetime: timedelta = ASSERTION_MAX_LIFETIME) -> dict[str, Any]:
    """Revendications de l'assertion JWT Google — PURE, la signature reste à l'appelant.

    `subject` porte la DÉLÉGATION : sans lui, le service account n'agit que pour lui-même et
    ne voit aucun artefact de réunion. C'est l'oubli le plus fréquent, et son symptôme est un
    404 sur des ressources qui existent pourtant.
    """
    if not service_account_email:
        raise TokenError("adresse du service account requise")
    if not scopes:
        raise TokenError("au moins une portée est requise")
    if lifetime > ASSERTION_MAX_LIFETIME:
        raise TokenError(
            f"durée d'assertion {lifetime} > maximum de {ASSERTION_MAX_LIFETIME} imposé par Google")

    issued = int(now.astimezone(timezone.utc).timestamp())
    claims: dict[str, Any] = {
        "iss": service_account_email,
        "scope": " ".join(scopes),
        "aud": GOOGLE_TOKEN_URL,
        "iat": issued,
        "exp": issued + int(lifetime.total_seconds()),
    }
    if subject:
        claims["sub"] = subject
    return claims


def google_token_request(signed_assertion: str) -> tuple[str, dict[str, str]]:
    """(URL, formulaire) pour échanger une assertion signée contre un jeton. PURE."""
    if not signed_assertion:
        raise TokenError("assertion signée vide")
    return GOOGLE_TOKEN_URL, {
        "grant_type": GOOGLE_JWT_BEARER_GRANT,
        "assertion": signed_assertion,
    }


@dataclass(frozen=True)
class AccessToken:
    """Un jeton et son échéance — l'échéance est ABSOLUE, pas une durée résiduelle.

    Conserver `expires_in` tel quel obligerait à savoir quand il a été reçu ; une échéance
    absolue se compare directement, y compris après un redémarrage.
    """

    value: str
    expires_at: datetime

    def needs_refresh(self, now: datetime, *, margin: timedelta = REFRESH_MARGIN) -> bool:
        """Faut-il le renouveler ? Vrai dès qu'on entre dans la marge."""
        return self.expires_at.astimezone(timezone.utc) - now.astimezone(timezone.utc) <= margin


def parse_token_response(payload: Any, *, now: datetime) -> AccessToken:
    """Réponse du serveur d'autorisation → jeton daté. PURE, donc testée.

    Les deux plateformes rendent la même forme : `access_token` et `expires_in` en secondes.
    Une réponse sans l'un des deux est une ERREUR et non un jeton sans échéance — supposer une
    durée par défaut ferait utiliser un jeton mort en croyant qu'il est valide.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise TokenError("réponse de jeton illisible (JSON attendu)") from exc
    if not isinstance(payload, dict):
        raise TokenError("réponse de jeton inexploitable")

    if payload.get("error"):
        raise TokenError(
            f"refus du serveur d'autorisation : {payload.get('error')} — "
            f"{str(payload.get('error_description') or '')[:200]}")

    token = str(payload.get("access_token") or "")
    if not token:
        raise TokenError("réponse sans « access_token »")
    try:
        expires_in = int(payload["expires_in"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError(
            "réponse sans « expires_in » exploitable : impossible de savoir quand renouveler, "
            "et supposer une durée ferait utiliser un jeton mort") from exc
    if expires_in <= 0:
        raise TokenError(f"durée de validité absurde : {expires_in}s")

    return AccessToken(value=token,
                       expires_at=now.astimezone(timezone.utc) + timedelta(seconds=expires_in))


def should_request_new_token(current: AccessToken | None, now: datetime, *,
                             margin: timedelta = REFRESH_MARGIN) -> bool:
    """Faut-il demander un jeton ? Vrai s'il n'y en a pas, ou s'il entre dans la marge.

    Cette fonction existe pour que la couche réseau n'ait aucune décision à prendre : elle
    demande, elle stocke, elle rejoue. Toute la règle est ici, donc testable.
    """
    return current is None or current.needs_refresh(now, margin=margin)

"""Couche RÉSEAU des abonnements Google Workspace Events — ce qui manquait à Meet.

`meet_events.py` sait CONSTRUIRE une demande d'abonnement et LIRE un message Pub/Sub ;
`pubsub_pull.py` sait construire une interrogation. Personne n'appelait Google. C'est le
« il reste les appels réseau » du §7-quinquies de `docs/TEMPS_REEL_REUNIONS.md`.

DÉCOUPAGE, comme partout ici : les fonctions `*_call` sont PURES (URL + corps + méthode) et
se testent sans compte ; `WorkspaceEventsClient` ne fait que porter le jeton et le transport,
lui-même INJECTABLE. Aucune dépendance nouvelle : `urllib` suffit pour quelques appels par
réunion, et `google-auth` fournit déjà le jeton (`oauth.GoogleOAuth`).

DEUX PIÈGES DE CETTE API, tous deux vérifiés à la source le 2026-08-01 :

1. **La création rend une `Operation`, pas un abonnement.** Elle peut être `done: false` :
   traiter la réponse comme un abonnement donnerait un objet sans `name`, et le code
   appelant croirait à un abonnement créé qu'il ne saurait plus ni renouveler ni supprimer.
2. **`validateOnly=true` existe** — la demande est vérifiée et prévisualisée SANS rien créer.
   C'est le seul moyen d'éprouver une forme (ressource cible, types d'évènements) contre le
   vrai service sans laisser derrière soi des abonnements orphelins qui, eux, expirent en
   sept jours et continuent de publier entre-temps.
"""
from __future__ import annotations

import json
from typing import Any

WORKSPACE_EVENTS_BASE = "https://workspaceevents.googleapis.com/v1"

# Portée suffisante pour s'abonner aux évènements Meet — la MOINS sensible des deux admises
# (`meetings.space.created` n'ouvrirait que les espaces créés par l'application, et donnerait
# en prime un droit d'écriture dont nous n'avons aucun usage). Relevé sur la référence REST
# de `subscriptions.create` et sur le guide d'autorisation, le 2026-08-01.
MEET_SUBSCRIPTION_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"


class WorkspaceEventsError(RuntimeError):
    """Refus de l'API Workspace Events — message destiné à l'exploitant."""


def create_call(body: dict[str, Any], *, validate_only: bool = False) -> tuple[str, str, dict]:
    """(méthode, URL, corps) d'une création d'abonnement. PURE."""
    url = f"{WORKSPACE_EVENTS_BASE}/subscriptions"
    if validate_only:
        url += "?validateOnly=true"
    return "POST", url, body


def list_call(filtre: str) -> tuple[str, str, None]:
    """(méthode, URL, corps) d'un inventaire. PURE.

    Le filtre est OBLIGATOIRE et doit porter sur `event_types` ou `target_resource` —
    constaté contre le service réel le 2026-08-01 : sans lui, Google répond
    « Invalid or unsupported query filter », sans dire qu'il en attendait un. On refuse donc
    ici plutôt que de laisser partir une requête qu'on sait vouée à l'échec.
    """
    import urllib.parse

    if not filtre.strip():
        raise WorkspaceEventsError(
            'filtre obligatoire — ex. event_types:"google.workspace.meet.recording.v2.'
            'fileGenerated" ou target_resource="//meet.googleapis.com/spaces/…"')
    url = f"{WORKSPACE_EVENTS_BASE}/subscriptions?" + urllib.parse.urlencode({"filter": filtre})
    return "GET", url, None


def patch_call(name: str, ttl: str = "0s") -> tuple[str, str, dict]:
    """(méthode, URL, corps) d'un RENOUVELLEMENT. PURE.

    `updateMask=ttl` est explicite et RESTREINT : la référence prévient que le joker `*`
    équivaut à un PUT et vide tout champ omis. Renouveler ne doit toucher qu'à l'échéance —
    surtout pas effacer par inadvertance les types d'évènements de l'abonnement.

    `ttl: "0s"` demande le MAXIMUM permis (sept jours) : c'est la forme documentée, et elle
    évite de recalculer une échéance à chaque tour.
    """
    import urllib.parse

    _check_name(name)
    url = (f"{WORKSPACE_EVENTS_BASE}/{name}?"
           + urllib.parse.urlencode({"updateMask": "ttl"}))
    return "PATCH", url, {"ttl": ttl}


def reactivate_call(name: str) -> tuple[str, str, dict]:
    """(méthode, URL, corps) d'une RÉACTIVATION. PURE.

    Ne vaut que pour un abonnement SUSPENDU, et seulement une fois la cause de la suspension
    levée : la référence précise que la méthode « ignore ou rejette » tout abonnement qui ne
    l'est pas. L'appeler à tout hasard ne répare donc rien — d'où la décision prise en amont
    par `subscription_renewal`, sur l'ÉTAT, et pas ici.
    """
    _check_name(name)
    return "POST", f"{WORKSPACE_EVENTS_BASE}/{name}:reactivate", {}


def _check_name(name: str) -> None:
    if not name.startswith("subscriptions/"):
        raise WorkspaceEventsError(
            f"nom d'abonnement invalide : {name!r} — attendu « subscriptions/… »")


def delete_call(name: str) -> tuple[str, str, None]:
    """(méthode, URL, corps) d'une suppression. PURE."""
    _check_name(name)
    return "DELETE", f"{WORKSPACE_EVENTS_BASE}/{name}", None


def subscription_of_operation(payload: Any) -> dict[str, Any]:
    """`Operation` → abonnement créé. Lève si l'opération a échoué ou n'est pas terminée.

    L'API répond par une opération de longue durée. En pratique elle arrive `done: true`,
    mais s'appuyer là-dessus sans le vérifier fabriquerait un abonnement fantôme : connu de
    Google, inconnu de nous, donc jamais renouvelé ni supprimé.
    """
    if not isinstance(payload, dict):
        raise WorkspaceEventsError("réponse inexploitable (objet attendu)")
    if payload.get("error"):
        erreur = payload["error"]
        detail = erreur.get("message") if isinstance(erreur, dict) else erreur
        raise WorkspaceEventsError(f"refus de Google : {str(detail)[:300]}")
    # `validateOnly` rend l'abonnement prévisualisé directement, sans enveloppe d'opération.
    if payload.get("name", "").startswith("subscriptions/"):
        return payload
    if not payload.get("done"):
        raise WorkspaceEventsError(
            f"abonnement PAS ENCORE créé — opération {payload.get('name') or '?'} en cours. "
            f"Ne pas le considérer comme acquis : il ne serait ni renouvelé ni supprimé.")
    reponse = payload.get("response")
    if not isinstance(reponse, dict) or not reponse.get("name"):
        raise WorkspaceEventsError("opération terminée sans abonnement dans la réponse")
    return reponse


def subscriptions_of_list(payload: Any) -> list[dict[str, Any]]:
    """Réponse d'inventaire → abonnements. Une réponse VIDE est normale (aucun abonnement)."""
    if not isinstance(payload, dict):
        raise WorkspaceEventsError("réponse inexploitable (objet attendu)")
    if payload.get("error"):
        erreur = payload["error"]
        detail = erreur.get("message") if isinstance(erreur, dict) else erreur
        raise WorkspaceEventsError(f"refus de Google : {str(detail)[:300]}")
    abonnements = payload.get("subscriptions") or []
    return [a for a in abonnements if isinstance(a, dict)]


def default_transport(method: str, url: str, body: dict | None,
                      headers: dict[str, str]) -> tuple[int, str]:
    """Transport `urllib` par défaut — remplacé par les tests."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class WorkspaceEventsClient:
    """Appels d'abonnement, jeton et transport injectés.

    `token_fn` rend un jeton d'accès DÉLÉGUÉ (l'utilisateur dont on suit les réunions) :
    la ressource cible est une donnée d'utilisateur, elle n'est pas visible du compte de
    service agissant pour lui-même.
    """

    def __init__(self, token_fn, transport=default_transport) -> None:
        self._token_fn = token_fn
        self._transport = transport

    def _appel(self, method: str, url: str, body: dict | None) -> Any:
        entetes = {"Authorization": f"Bearer {self._token_fn()}",
                   "Content-Type": "application/json"}
        try:
            statut, charge = self._transport(method, url, body, entetes)
        except Exception as exc:  # noqa: BLE001 — réseau : erreur typée, jamais une fuite
            raise WorkspaceEventsError(
                f"Google injoignable ({exc.__class__.__name__}) — réseau/proxy ?") from exc
        try:
            donnees = json.loads(charge or "{}")
        except ValueError:
            raise WorkspaceEventsError(
                f"réponse illisible (HTTP {statut}) : {charge[:200]}") from None
        if statut >= 400:
            detail = donnees.get("error", {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise WorkspaceEventsError(f"HTTP {statut} — {message or charge[:200]}")
        return donnees

    def create(self, body: dict[str, Any], *, validate_only: bool = False) -> dict[str, Any]:
        method, url, corps = create_call(body, validate_only=validate_only)
        return subscription_of_operation(self._appel(method, url, corps))

    def list(self, filtre: str) -> list[dict[str, Any]]:
        method, url, corps = list_call(filtre)
        return subscriptions_of_list(self._appel(method, url, corps))

    def patch(self, name: str, ttl: str = "0s") -> dict[str, Any]:
        """Renouvelle — rend l'abonnement prolongé."""
        return subscription_of_operation(self._appel(*patch_call(name, ttl)))

    def reactivate(self, name: str) -> dict[str, Any]:
        """Relance un abonnement SUSPENDU — rend l'abonnement redevenu actif."""
        return subscription_of_operation(self._appel(*reactivate_call(name)))

    def delete(self, name: str) -> None:
        method, url, corps = delete_call(name)
        self._appel(method, url, corps)

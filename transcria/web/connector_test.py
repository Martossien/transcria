"""Tests de CONNEXION des identités de plateforme — bouton « Tester » des fiches
(`testable: true` du catalogue).

Zoom (General App / Meeting SDK) : le JWT SDK ne se valide pas à distance, mais le couple
Client ID/Secret se vérifie contre l'endpoint OAuth officiel (`https://zoom.us/oauth/token`,
Basic) — `invalid_client` = couple refusé ; toute réponse AUTHENTIFIÉE (200, ou une erreur
de grant qui suppose l'authentification passée) = couple valide. Vérifié contre la doc
officielle (developers.zoom.us/docs/meeting-sdk/get-credentials, 2026-07).

PUR au sens réseau-injecté : `opener` remplaçable par les tests. Jamais un secret dans les
messages ni les logs.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"


def _default_opener(url: str, data: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check_zoom_credentials(client_id: str, client_secret: str,
                          opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict lisible). ok=True SEULEMENT si Zoom a authentifié le couple."""
    if not client_id or not client_secret:
        return False, ("identifiants incomplets — renseigner Client ID et Client Secret "
                       "(fiche Zoom, ou environnement du runner)")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        status, body = opener(
            _ZOOM_OAUTH_URL, b"grant_type=client_credentials",
            {"Authorization": f"Basic {basic}",
             "Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001 — réseau : verdict, jamais une levée
        return False, f"Zoom injoignable ({exc.__class__.__name__}) — vérifier le réseau/proxy"
    try:
        payload = json.loads(body or "{}")
    except ValueError:
        payload = {}
    error = str(payload.get("error") or payload.get("errorCode") or "")
    if status == 200:
        return True, "identifiants VALIDES — Zoom a délivré un jeton"
    if error == "invalid_client" or status == 401:
        return False, ("identifiants REFUSÉS par Zoom (invalid_client) — vérifier Client "
                       "ID/Secret (jeu « Development » de l'app, Basic Information)")
    if error:
        # Erreur de GRANT (ex. unsupported_grant_type) : l'authentification, elle, a passé.
        return True, f"identifiants valides (Zoom a répondu authentifié : {error})"
    return False, f"réponse Zoom inattendue (HTTP {status}) — voir les logs du portail"


_ENTRA_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# EN DUR, alors que le fichier de compte de service porte `token_uri` — délibéré, et vérifié
# aux deux sources le 2026-08-01 :
#   · la doc du flux JWT-bearer prescrit cette valeur pour `aud` (« this value is always
#     https://oauth2.googleapis.com/token ») ;
#   · `google-auth`, qui fera l'authentification RÉELLE côté service connecteur, lit bien
#     `token_uri` du fichier — mais tout fichier de l'univers public porte cette même URL,
#     et un `universe_domain` non standard n'y provoque pas d'erreur : la bibliothèque bascule
#     alors en JWT auto-signé (`_always_use_jwt_access`), sans passer par ce point d'entrée.
# Conséquence : les deux chemins coïncident pour toute clé googleapis.com. Ils divergeraient
# pour un cloud souverain — le verdict de ce test ne décrirait alors plus ce que fait le
# service. Le jour où ce cas se présente, lire `token_uri` du fichier plutôt que d'élargir
# cette constante.
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def check_teams_credentials(tenant_id: str, client_id: str, client_secret: str,
                            opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict) — jeton APPLICATIF Entra ID (client_credentials, scope Graph).

    Prouve locataire + client + secret SANS abonnement ni réunion. Ne dit RIEN des
    permissions ni de la politique d'accès applicatif (`New-CsApplicationAccessPolicy`) :
    ces deux-là sont les pannes MUETTES documentées — le verdict le rappelle.
    Vérifié contre la doc officielle (2026-07-31)."""
    if not (tenant_id and client_id and client_secret):
        return False, ("identifiants incomplets — locataire, client et secret requis "
                       "(fiche Teams)")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default"}).encode()
    try:
        status, payload = opener(_ENTRA_TOKEN_URL.format(tenant=tenant_id), body,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001
        return False, f"Entra ID injoignable ({exc.__class__.__name__}) — réseau/proxy ?"
    data = _json_or_empty(payload)
    if status == 200 and data.get("access_token"):
        return True, ("jeton applicatif obtenu — identifiants VALIDES. Restent à vérifier "
                      "à la main : permission OnlineMeetingRecording.Read.All consentie, "
                      "et politique d'accès applicatif (New-CsApplicationAccessPolicy) "
                      "sans laquelle aucun enregistrement n'est visible")
    error = str(data.get("error") or "")
    hints = {"invalid_client": "secret client erroné ou expiré",
             "unauthorized_client": "application inconnue de ce locataire",
             "invalid_request": "identifiant de locataire invalide"}
    return False, (f"Entra ID a REFUSÉ ({error or f'HTTP {status}'})"
                   + (f" — {hints[error]}" if error in hints else ""))


# DEUX identités, deux mécanismes d'autorisation — la distinction est tout sauf cosmétique.
#
# Ces portées-ci portent sur des données d'UTILISATEUR (les espaces Meet, le Drive de
# l'organisateur) : elles s'obtiennent en IMPERSONNANT cet utilisateur, ce qu'autorise la
# délégation à l'échelle du domaine, dans la console Admin.
_MEET_DELEGATED_SCOPES = (
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
# Pub/Sub, lui, n'est pas une donnée d'utilisateur : la file appartient au projet Cloud. Le
# compte de service l'interroge EN SON PROPRE NOM, et son droit vient de Cloud IAM
# (`roles/pubsub.subscriber` SUR L'ABONNEMENT) — pas de la délégation. Exiger cette portée
# dans la délégation, comme nous le faisions, obligeait l'administrateur Workspace à accorder
# un droit qui ne le concerne pas, et faisait échouer la demande ENTIÈRE quand il ne l'avait
# pas fait — Google refuse en bloc, il n'accorde jamais une partie des portées demandées.
_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


class _MeetTokenUnreachable(RuntimeError):
    """Google injoignable — à distinguer d'un refus, qui est une réponse."""


def _meet_token_attempt(key: dict, impersonate: str, scopes: tuple[str, ...],
                        opener) -> tuple[int, dict]:
    """Une demande de jeton par assertion signée. Rend (statut, corps JSON).

    `impersonate` vide = demande POUR LE COMPTE DE SERVICE LUI-MÊME (pas de revendication
    `sub`) : c'est le témoin qui sépare « la clé et le compte sont bons » de « l'utilisateur
    représenté est refusé ».
    """
    import time

    import jwt  # dép opt-in des connecteurs (PyJWT[crypto]) — présence vérifiée par l'appelant

    now = int(time.time())
    revendications = {
        "iss": key["client_email"], "scope": " ".join(scopes),
        "aud": _GOOGLE_TOKEN_URL, "iat": now, "exp": now + 3600}
    if impersonate:
        revendications["sub"] = impersonate
    assertion = jwt.encode(revendications, key["private_key"], algorithm="RS256")
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion}).encode()
    try:
        status, payload = opener(_GOOGLE_TOKEN_URL, body,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001
        raise _MeetTokenUnreachable(exc.__class__.__name__) from exc
    return status, _json_or_empty(payload)


def _diagnose_unauthorized(key: dict, impersonate: str, detail: str, opener) -> str:
    """Pourquoi `unauthorized_client` ? Reprend la demande PORTÉE PAR PORTÉE.

    Google refuse en bloc et d'un seul message, que la délégation soit absente ou qu'une
    seule portée sur trois manque à la liste — deux causes qui n'appellent pas du tout le
    même geste. En rejouant chaque portée seule, on sait laquelle est refusée : c'est la
    différence entre « créez la délégation » et « ajoutez cette ligne-ci ».
    """
    identifiant = str(key.get("client_id") or "")
    rappel = (f" La délégation s'enregistre avec le Client ID NUMÉRIQUE {identifiant}"
              if identifiant else "")
    accordees: list[str] = []
    refusees: list[str] = []
    for portee in _MEET_DELEGATED_SCOPES:
        try:
            statut, corps = _meet_token_attempt(key, impersonate, (portee,), opener)
        except _MeetTokenUnreachable:
            return ("Google a REFUSÉ (unauthorized_client) puis est devenu injoignable — "
                    "diagnostic par portée impossible." + rappel)
        (accordees if statut == 200 and corps.get("access_token") else refusees).append(
            portee.rsplit("/", 1)[1])
    if not accordees:
        return ("Google a REFUSÉ (unauthorized_client) et AUCUNE portée n'est accordée — "
                "la délégation à l'échelle du domaine n'existe pas pour ce compte de "
                "service, vise un autre identifiant, ou n'est pas encore propagée "
                "(jusqu'à 24 h, en général quelques minutes)." + rappel
                + (f" Google précise : {detail[:100]}" if detail else ""))
    return (f"Google a REFUSÉ (unauthorized_client) — la délégation EXISTE (accordé : "
            f"{', '.join(accordees)}) mais il manque : {', '.join(refusees)}. "
            f"Ajouter ces portées à la même ligne de délégation." + rappel)


def _diagnose_principal(key: dict, impersonate: str, detail: str, opener) -> str:
    """« Invalid principal » : le refus porte-t-il sur la CLÉ ou sur l'utilisateur REPRÉSENTÉ ?

    On rejoue la demande sans revendication `sub`, c'est-à-dire pour le compte de service
    seul. S'il obtient un jeton, la clé et le compte sont hors de cause et le refus vise
    l'adresse impersonnée — message qui, tel que Google le rend, ne dit pas laquelle des
    deux identités il rejette.
    """
    try:
        statut, corps = _meet_token_attempt(key, "", _MEET_DELEGATED_SCOPES, opener)
    except _MeetTokenUnreachable:
        return f"Google a REFUSÉ (principal invalide) {detail[:120]}".strip()
    if statut == 200 and corps.get("access_token"):
        return (f"Google a REFUSÉ l'utilisateur REPRÉSENTÉ « {impersonate} » — la clé et le "
                f"compte de service sont bons (jeton obtenu sans impersonation). Cette "
                f"adresse doit être un utilisateur RÉEL du domaine Workspace délégué : "
                f"vérifier l'orthographe, le domaine, et qu'il ne s'agit pas d'un simple "
                f"alias ni d'un compte non provisionné.")
    return (f"Google a REFUSÉ (principal invalide) même SANS impersonation — le refus porte "
            f"sur le compte de service lui-même (adresse « {key.get('client_email', '?')} » "
            f"supprimée ou désactivée ? clé révoquée ?). {detail[:100]}").strip()


def _pubsub_note(key: dict, opener) -> str:
    """Complément du verdict : le compte de service peut-il obtenir un jeton Pub/Sub SEUL ?

    Sans impersonation, puisque c'est ainsi que la file sera interrogée. Ce que cela prouve
    est modeste — Google délivre le jeton, les DROITS ne sont éprouvés qu'au premier appel —
    et le verdict le dit plutôt que de laisser croire à une validation complète.
    """
    try:
        statut, corps = _meet_token_attempt(key, "", (_PUBSUB_SCOPE,), opener)
    except _MeetTokenUnreachable:
        statut, corps = 0, {}
    compte = key.get("client_email", "le compte de service")
    if not (statut == 200 and corps.get("access_token")):
        return ("En revanche le compte de service n'obtient PAS de jeton Pub/Sub en son "
                "propre nom — la file ne pourra pas être interrogée.")
    return (f"Restent deux droits que ce test ne peut PAS voir, tous deux silencieux : "
            f"« Pub/Sub Publisher » pour meet-api-event-push@system.gserviceaccount.com SUR "
            f"LE SUJET (sans quoi la file reste vide sans erreur), et « Pub/Sub Subscriber » "
            f"pour {compte} SUR L'ABONNEMENT (sans quoi l'interrogation est refusée). Ces "
            f"deux-là sont dans Cloud IAM, PAS dans la délégation Workspace.")


def check_meet_credentials(service_account_json: str, impersonate: str,
                           opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict) — jeton Google par assertion signée du compte de service.

    Prouve la clé et la DÉLÉGATION à l'échelle du domaine (l'impersonation échoue sans
    elle) SANS réunion. Ne dit rien du rôle Pub/Sub Publisher accordé à
    `meet-api-event-push@system.gserviceaccount.com` — panne muette n°1, rappelée."""
    if not service_account_json or not impersonate:
        return False, ("identifiants incomplets — clé JSON du compte de service et "
                       "utilisateur à impersonner requis (fiche Meet)")
    if "@" not in impersonate:
        # Vécu le 2026-08-01 : l'ID client NUMÉRIQUE du compte de service posé ici. Il n'a
        # qu'un usage, la ligne de délégation dans la console Admin — et les deux champs se
        # remplissent dans la même demi-heure. Google répond « Invalid principal », qui ne
        # désigne rien ; le dire ici épargne l'aller-retour et la fausse piste.
        return False, (f"« {impersonate} » n'est pas une adresse — ce champ attend "
                       f"l'utilisateur à REPRÉSENTER (ex. admin@votre-domaine). L'ID client "
                       f"numérique du compte de service, lui, ne sert QUE dans la ligne de "
                       f"délégation de la console Admin.")
    try:
        key = json.loads(Path(service_account_json).read_text(encoding="utf-8")
                         if not service_account_json.lstrip().startswith("{")
                         else service_account_json)
    except (OSError, ValueError) as exc:
        return False, f"clé de compte de service illisible ({exc.__class__.__name__})"
    try:
        import jwt  # noqa: F401 — présence contrôlée ici, utilisée par _meet_token_attempt
    except ImportError:
        return False, ("PyJWT absent — installer les dépendances connecteurs "
                       "(requirements-connectors.txt)")
    try:
        status, data = _meet_token_attempt(key, impersonate, _MEET_DELEGATED_SCOPES, opener)
    except _MeetTokenUnreachable as exc:
        return False, f"Google injoignable ({exc}) — réseau/proxy ?"
    except Exception as exc:  # noqa: BLE001 — clé malformée : la signature échoue
        return False, f"signature impossible ({exc.__class__.__name__}) — clé invalide ?"
    if status == 200 and data.get("access_token"):
        return True, ("jeton obtenu par délégation — clé et délégation de domaine VALIDES. "
                      + _pubsub_note(key, opener))
    error = str(data.get("error") or "")
    detail = str(data.get("error_description") or "")
    if error == "unauthorized_client":
        return False, _diagnose_unauthorized(key, impersonate, detail, opener)
    if "principal" in detail.lower():
        return False, _diagnose_principal(key, impersonate, detail, opener)
    return False, f"Google a REFUSÉ ({error or f'HTTP {status}'}) {detail[:120]}".strip()


def _json_or_empty(payload: str) -> dict:
    try:
        data = json.loads(payload or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}

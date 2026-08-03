"""Abonnements Microsoft Graph pour les ENREGISTREMENTS de réunion Teams — parties PURES.

Voie retenue pour Teams après étude comparée des trois possibles
(cf. docs/TEMPS_REEL_REUNIONS.md §7-ter) : ni SDK média (Windows Server EN AZURE + IP publique
par instance), ni bot navigateur (~4 700 lignes chez les références, captcha, et Microsoft
resserre), mais les **notifications de changement Graph** — officielles, sans bot.

CIBLE : les ENREGISTREMENTS, pas les transcriptions. L'API de transcription rend le texte
produit par Teams, alors que notre valeur est notre propre chaîne (STT, diarisation, LLM).
`recordingContentUrl` rend le MP4, que nous traitons nous-mêmes. Ce choix a un second
bénéfice, vérifié sur la documentation : le nouveau verrou de locataire « Transcript API
access » (imposé au 29 juillet 2026) ne concerne QUE les transcriptions — *« recording
subscriptions are unaffected »*.

Tout ce qui est ici est PUR et testé en CI. Le réseau (jeton OAuth, POST d'abonnement,
téléchargement) vit ailleurs et sera confirmé au gate, faute de locataire Microsoft 365 à ce
jour — c'est assumé et écrit, plutôt que déguisé en couverture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Chaînes de ressource EXACTES, relevées sur la documentation Graph. Elles ne se devinent pas
# et une faute de frappe se solde par un 400 peu bavard.
RECORDINGS_TENANT = "communications/onlineMeetings/getAllRecordings"
RECORDINGS_BY_ORGANIZER = "users/{user_id}/onlineMeetings/getAllRecordings"
RECORDINGS_BY_MEETING = "communications/onlineMeetings/{meeting_id}/recordings"
TRANSCRIPTS_TENANT = "communications/onlineMeetings/getAllTranscripts"

# Permission applicative correspondante, à faire consentir par l'admin du locataire.
PERMISSION_BY_RESOURCE = {
    RECORDINGS_TENANT: "OnlineMeetingRecording.Read.All",
    RECORDINGS_BY_ORGANIZER: "OnlineMeetingRecording.Read.All",
    RECORDINGS_BY_MEETING: "OnlineMeetingRecording.Read.All",
    TRANSCRIPTS_TENANT: "OnlineMeetingTranscript.Read.All",
}

# Au-delà d'une heure, Graph EXIGE une URL de notifications de cycle de vie, sinon la création
# échoue avec « lifecycleNotificationUrl is required for subscription creation on this
# resource when the expirationDateTime value exceeds 1 hour ». Piège classique : on demande
# une longue durée pour s'épargner les renouvellements, et rien ne se crée.
LIFECYCLE_REQUIRED_BEYOND = timedelta(hours=1)

# Durée de vie maximale, RELEVÉE sur la table officielle de Graph : 4 320 minutes pour
# `callRecording` comme pour `callTranscript`. Une première écriture avait supposé 24 h — la
# validation aurait alors rejeté un abonnement de deux jours parfaitement légitime. C'est
# exactement le genre de valeur qui ne se devine pas.
MAX_SUBSCRIPTION_LIFETIME = timedelta(minutes=4320)     # trois jours

# Marge de renouvellement. Elle n'est pas un excès de prudence : la latence de notification
# d'un enregistrement peut atteindre 60 minutes (table officielle), et un abonnement expiré
# ne se renouvelle PAS — il faut en recréer un.
RENEWAL_MARGIN = timedelta(minutes=90)

# Quotas relevés eux aussi, car ils décident de l'architecture : 1 abonnement par couple
# application/utilisateur, 10 par utilisateur, et 10 000 par organisation — ce dernier étant
# PARTAGÉ entre TOUTES les ressources Teams (discussions, messages, transcriptions…).
MAX_SUBSCRIPTIONS_PER_APP_AND_USER = 1
MAX_SUBSCRIPTIONS_PER_ORGANIZATION = 10_000

# Latence attendue entre la fin de réunion et la notification (table officielle).
NOTIFICATION_LATENCY_TYPICAL = timedelta(seconds=10)
NOTIFICATION_LATENCY_MAX = timedelta(minutes=60)


class GraphSubscriptionError(ValueError):
    """Demande d'abonnement incohérente — détectée AVANT l'appel réseau."""


def build_subscription_request(*, resource: str, notification_url: str,
                               client_state: str,
                               expires_at: datetime,
                               lifecycle_notification_url: str = "",
                               include_resource_data: bool = False,
                               encryption_certificate: str = "",
                               encryption_certificate_id: str = "") -> dict[str, Any]:
    """Corps d'un `POST /subscriptions` — fonction PURE, donc testable sans locataire.

    Les règles de Graph sont vérifiées ICI plutôt que découvertes dans un 400 :

    - au-delà d'une heure, `lifecycleNotificationUrl` est OBLIGATOIRE ;
    - `includeResourceData` impose un certificat de chiffrement ET son identifiant : sans
      eux, Graph accepte parfois la création puis n'envoie rien d'exploitable ;
    - `notificationUrl` doit être en HTTPS — Graph refuse tout le reste, et c'est aussi ce qui
      impose une entrée à travers le pare-feu du client.

    ⚠ `includeResourceData` reste FAUX par défaut. La documentation est explicite : sans lui,
    aucun certificat n'est nécessaire. On reçoit l'identifiant de l'enregistrement et on va
    chercher le contenu — une version bien plus simple à mettre en service, et une pièce de
    moins à faire gérer par l'administrateur du client.
    """
    if not resource:
        raise GraphSubscriptionError("ressource d'abonnement vide")
    if not notification_url.lower().startswith("https://"):
        raise GraphSubscriptionError(
            f"notificationUrl doit être en HTTPS (reçu : {notification_url!r}) — "
            f"Graph refuse tout autre schéma")
    if not client_state:
        raise GraphSubscriptionError(
            "clientState requis : c'est lui qui permet de reconnaître nos propres "
            "notifications d'un appel forgé")

    now = datetime.now(UTC)
    horizon = expires_at.astimezone(UTC) - now
    if horizon <= timedelta(0):
        raise GraphSubscriptionError("expirationDateTime déjà passée")
    if horizon > MAX_SUBSCRIPTION_LIFETIME:
        raise GraphSubscriptionError(
            f"durée demandée {horizon} > maximum admis {MAX_SUBSCRIPTION_LIFETIME}")
    if horizon > LIFECYCLE_REQUIRED_BEYOND and not lifecycle_notification_url:
        raise GraphSubscriptionError(
            "au-delà d'une heure, Graph exige lifecycleNotificationUrl — sans lui la "
            "création échoue, sans rapport apparent avec la durée demandée")

    if include_resource_data and not (encryption_certificate and encryption_certificate_id):
        raise GraphSubscriptionError(
            "includeResourceData exige un certificat de chiffrement ET son identifiant")

    body: dict[str, Any] = {
        "changeType": "created",
        "resource": resource,
        "notificationUrl": notification_url,
        "clientState": client_state,
        "expirationDateTime": _iso(expires_at),
        "includeResourceData": include_resource_data,
    }
    if lifecycle_notification_url:
        body["lifecycleNotificationUrl"] = lifecycle_notification_url
    if include_resource_data:
        body["encryptionCertificate"] = encryption_certificate
        body["encryptionCertificateId"] = encryption_certificate_id
    return body


def _iso(moment: datetime) -> str:
    """Horodatage au format attendu par Graph (UTC, suffixe Z)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def renewal_deadline(expires_at: datetime, *, margin: timedelta = RENEWAL_MARGIN) -> datetime:
    """Instant à partir duquel il faut renouveler.

    Renouveler à l'expiration exacte perd les notifications émises pendant l'aller-retour ;
    la marge est là pour ça, pas par excès de prudence.
    """
    return expires_at.astimezone(UTC) - margin


@dataclass(frozen=True)
class RecordingNotification:
    """Une notification d'enregistrement disponible, réduite à ce qui nous sert."""

    subscription_id: str
    resource: str
    recording_id: str
    client_state: str
    tenant_id: str

    @property
    def content_path(self) -> str:
        """Chemin Graph du CONTENU (le MP4), déduit de la ressource notifiée.

        Graph notifie la ressource sous une forme OData avec parenthèses et apostrophes
        (`onlineMeetings('MSo…')/recordings('VjI…')`). Le chemin de contenu s'obtient en
        ajoutant `/content` — mais la forme parenthésée doit être conservée telle quelle,
        les identifiants contenant des caractères qui ne survivent pas à une réécriture.
        """
        return f"{self.resource}/content"


def parse_notifications(payload: Any) -> list[RecordingNotification]:
    """Charge utile de webhook → notifications exploitables. PURE, donc testée.

    Tolérante par construction : une notification mal formée est ÉCARTÉE, jamais fatale. Le
    lot peut en contenir plusieurs, et perdre les autres parce que l'une est illisible serait
    le pire des comportements — d'autant que l'émetteur est hors de notre contrôle.
    """
    if not isinstance(payload, dict):
        return []
    notifications = []
    for raw in payload.get("value") or []:
        if not isinstance(raw, dict):
            continue
        resource = str(raw.get("resource") or "")
        data = raw.get("resourceData") or {}
        recording_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        if not resource or not recording_id:
            continue
        notifications.append(RecordingNotification(
            subscription_id=str(raw.get("subscriptionId") or ""),
            resource=resource,
            recording_id=recording_id,
            client_state=str(raw.get("clientState") or ""),
            tenant_id=str(raw.get("tenantId") or ""),
        ))
    return notifications


def transcript_access_disabled(error: Any) -> bool:
    """L'administrateur a-t-il coupé l'accès Graph aux transcriptions ?

    On teste le CODE d'erreur interne, pas le message : la documentation le demande
    explicitement — *« Branch on the innerError.code value, not the message text — messages
    are subject to change »*. Un test sur le texte casserait à la première reformulation.

    Ne concerne que les transcriptions ; les abonnements aux ENREGISTREMENTS, qui sont notre
    cible, ne sont pas affectés par ce réglage.
    """
    if not isinstance(error, dict):
        return False
    node: Any = error.get("error", error)
    while isinstance(node, dict):
        if str(node.get("code") or "") == "GraphAccessToTranscriptsDisabled":
            return True
        node = node.get("innerError")
    return False


def client_state_matches(notification: RecordingNotification, expected: str) -> bool:
    """Compare le `clientState` reçu à celui posé à la création de l'abonnement.

    Première barrière contre un appel forgé : n'importe qui connaissant l'URL peut POSTer.
    Elle ne remplace PAS la vérification des `validationTokens` signés par Microsoft, qui
    relève de la couche réseau — les deux se cumulent.
    """
    return bool(expected) and notification.client_state == expected


# --------------------------------------------------------------------------- #
#  Notifications de CYCLE DE VIE — sans elles, le flux s'arrête sans bruit
# --------------------------------------------------------------------------- #
# Trois valeurs possibles, et trois seulement (documentation Graph). Les ignorer casse le flux
# de notifications : c'est le mode de panne le plus sournois, puisque tout paraît en place.
LIFECYCLE_REAUTHORIZATION = "reauthorizationRequired"
LIFECYCLE_SUBSCRIPTION_REMOVED = "subscriptionRemoved"
LIFECYCLE_MISSED = "missed"


@dataclass(frozen=True)
class LifecycleAction:
    """Ce qu'il faut FAIRE, et pourquoi — le nom de l'évènement seul n'aide personne."""

    action: str          # "renew" | "recreate" | "resync" | "ignore"
    reason: str


# Conduites tirées de la documentation, pas devinées.
_LIFECYCLE_ACTIONS = {
    LIFECYCLE_REAUTHORIZATION: LifecycleAction(
        "renew",
        "jeton ou abonnement sur le point d'expirer : réautoriser. Un seul PATCH avec une "
        "nouvelle expirationDateTime fait les deux — ⚠ ne JAMAIS enchaîner /reauthorize et "
        "PATCH sur le même abonnement en moins de dix minutes, l'état devient incohérent"),
    LIFECYCLE_SUBSCRIPTION_REMOVED: LifecycleAction(
        "recreate",
        "Graph a supprimé l'abonnement : en recréer un. Les notifications émises entre-temps "
        "sont perdues et doivent être rattrapées autrement"),
    LIFECYCLE_MISSED: LifecycleAction(
        "resync",
        "des notifications n'ont pas été délivrées (limitation de débit) : resynchroniser "
        "la ressource pour retrouver ce qui manque"),
}


def parse_lifecycle_events(payload: Any) -> list[tuple[str, str]]:
    """Charge utile de cycle de vie → [(subscriptionId, lifecycleEvent)]. PURE.

    Un lot peut mêler PLUSIEURS évènements de natures différentes : la documentation le dit,
    et n'en traiter qu'un laisserait les autres sans réponse.
    """
    if not isinstance(payload, dict):
        return []
    events = []
    for raw in payload.get("value") or []:
        if not isinstance(raw, dict):
            continue
        event = str(raw.get("lifecycleEvent") or "")
        if event:
            events.append((str(raw.get("subscriptionId") or ""), event))
    return events


def lifecycle_action(event: str) -> LifecycleAction:
    """Conduite à tenir face à un évènement de cycle de vie.

    Un évènement INCONNU n'est pas ignoré en silence : on le signale comme tel, car il
    signifierait que Graph a introduit un cas que nous ne savons pas traiter — et le flux
    s'arrêterait sans que rien ne l'explique.
    """
    known = _LIFECYCLE_ACTIONS.get(event)
    if known is not None:
        return known
    return LifecycleAction("ignore", f"évènement de cycle de vie inconnu : {event!r} — "
                                     f"à vérifier contre la documentation Graph")

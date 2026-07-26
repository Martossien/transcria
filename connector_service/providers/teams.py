"""Adaptateur Teams post-réunion — Microsoft Graph (A3, ADR-001 D8).
from connector_service.http_defaults import DEFAULT_HTTP_TIMEOUT_S

Teams notifie (change notification chiffrée) la création d'un `callRecording` (ou
`callTranscript`). On traduit la ressource en occurrence + artefact ; le contenu se
récupère via Graph `…/recordings/{id}/content` (Bearer) — fetcher distinct (plomberie
ultérieure). Le transcript Teams (VTT) reste un artefact AUXILIAIRE (ADR-001 D7).

⚠️ Forme d'après la doc Graph (learn.microsoft.com) ; à confirmer contre un tenant M365
réel au gate manuel. Parsing tolérant (une notification porte une liste `value`).
"""
from __future__ import annotations

from dataclasses import dataclass

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact
from connector_service.http_defaults import DEFAULT_HTTP_TIMEOUT_S

PROVIDER = "teams"


class TeamsNotificationError(ValueError):
    """Notification Graph invalide ou sans ressource enregistrement exploitable."""


@dataclass(frozen=True)
class TeamsRecording:
    tenant_meeting_id: str        # onlineMeeting id = occurrence
    organizer_id: str
    recording_id: str
    resource_path: str            # "communications/onlineMeetings('…')/recordings('…')"
    change_type: str

    @classmethod
    def from_notification(cls, payload: dict) -> TeamsRecording:
        """Prend une change notification Graph ({"value": [ {resource, resourceData,
        changeType}, … ]}) et en extrait la 1re ressource callRecording créée."""
        if not isinstance(payload, dict):
            raise TeamsNotificationError("notification Teams invalide (objet attendu)")
        items = payload.get("value")
        if not isinstance(items, list) or not items:
            raise TeamsNotificationError("notification Teams sans entrée 'value'")
        for item in items:
            data = (item or {}).get("resourceData") or {}
            odata = str(data.get("@odata.type") or "")
            if "callRecording" not in odata and "/recordings(" not in str(item.get("resource") or ""):
                continue
            meeting_id = str(data.get("meetingId") or "").strip()
            rec_id = str(data.get("id") or "").strip()
            if not meeting_id or not rec_id:
                continue
            return cls(
                tenant_meeting_id=meeting_id,
                organizer_id=str(data.get("meetingOrganizerId") or ""),
                recording_id=rec_id,
                resource_path=str(item.get("resource") or ""),
                change_type=str(item.get("changeType") or ""),
            )
        raise TeamsNotificationError("aucune ressource callRecording exploitable")


class TeamsRecordingAdapter:
    #: Base Graph pour construire l'URL de contenu (le fetcher y ajoutera /content + Bearer).
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def to_occurrence(self, rec: TeamsRecording) -> ExternalMeetingOccurrence:
        return ExternalMeetingOccurrence(
            provider=PROVIDER,
            provider_account_id=rec.organizer_id,
            external_occurrence_id=rec.tenant_meeting_id,
            organizer=rec.organizer_id,
        )

    def to_artifact(self, rec: TeamsRecording) -> RemoteArtifact:
        return RemoteArtifact(
            artifact_id=rec.recording_id,
            storage_uri=f"{self.GRAPH_BASE}/{rec.resource_path}/content"
            if rec.resource_path else f"graph:recording:{rec.recording_id}",
            media_type="video/mp4",
            artifact_type="recording",
        )

    def dedup_key(self, rec: TeamsRecording) -> str:
        return "|".join((PROVIDER, rec.organizer_id, rec.tenant_meeting_id, rec.recording_id))


def _requests_json(method: str, url: str, *, headers: dict, json_body: dict | None,
                   timeout_s: float = DEFAULT_HTTP_TIMEOUT_S) -> tuple[int, dict]:
    import requests  # dép TranscrIA

    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout_s)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


class TeamsSubscriptionManager:
    """Cycle de vie des abonnements Graph (create/renew). Sans renouvellement, Graph cesse
    de notifier — le lifecycle `reauthorizationRequired` (reçu par le récepteur) déclenche
    `renew`. `oauth` = MicrosoftOAuth ; `http` injectable (CI mockée)."""

    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, oauth, *, notification_url: str, lifecycle_url: str, resource: str,
                 client_state: str, encryption_cert_id: str = "", encryption_cert_b64: str = "",
                 http=None) -> None:
        self._oauth = oauth
        self._notif = notification_url
        self._lifecycle = lifecycle_url
        self._resource = resource
        self._client_state = client_state
        self._cert_id = encryption_cert_id
        self._cert = encryption_cert_b64
        self._http = http or _requests_json

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._oauth.token()}", "Content-Type": "application/json"}

    def create(self, *, expiration_iso: str, change_type: str = "created") -> dict:
        body: dict = {
            "changeType": change_type,
            "notificationUrl": self._notif,
            "lifecycleNotificationUrl": self._lifecycle,
            "resource": self._resource,
            "expirationDateTime": expiration_iso,
            "clientState": self._client_state,
        }
        if self._cert and self._cert_id:      # rich notifications (resource data chiffrée)
            body.update({"includeResourceData": True,
                         "encryptionCertificate": self._cert,
                         "encryptionCertificateId": self._cert_id})
        status, resp = self._http("POST", f"{self.GRAPH}/subscriptions",
                                  headers=self._auth(), json_body=body)
        if status not in (200, 201):
            raise TeamsNotificationError(f"création d'abonnement Teams échouée (status={status})")
        return resp

    def renew(self, subscription_id: str, *, expiration_iso: str) -> dict:
        status, resp = self._http("PATCH", f"{self.GRAPH}/subscriptions/{subscription_id}",
                                  headers=self._auth(),
                                  json_body={"expirationDateTime": expiration_iso})
        if status != 200:
            raise TeamsNotificationError(f"renouvellement d'abonnement Teams échoué (status={status})")
        return resp


def lifecycle_subscription_ids(payload: dict, *, event: str = "reauthorizationRequired") -> list[str]:
    """Extrait les subscriptionId d'une notification de CYCLE DE VIE (à renouveler)."""
    return [str((v or {}).get("subscriptionId") or "")
            for v in (payload.get("value") or [])
            if isinstance(v, dict) and v.get("lifecycleEvent") == event and v.get("subscriptionId")]

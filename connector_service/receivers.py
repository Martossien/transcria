"""Récepteurs webhook par plateforme (A2/A3) — routes Flask enregistrables.

Chaque récepteur : vérifie l'authenticité (signature Zoom / abonnement + déchiffrement
Teams), gère le défi de validation, puis délègue au handler d'ingestion async (via
`asyncio.run`). Le `handler` est INJECTÉ → testable avec un handler factice, sans réseau.

- **Zoom** : défi `endpoint.url_validation` + signature HMAC ; le `download_token` du
  téléchargement vit DANS l'événement.
- **Teams** : validation d'abonnement (echo `validationToken`) + notifications (le
  déchiffrement du contenu riche est disponible via `signatures.decrypt_teams_content`,
  optionnel — le `resourceData` porte déjà id + meetingId).
- **Meet** : pas de récepteur — modèle POLL (ProviderReconciler sur conferenceRecords).
"""
from __future__ import annotations

import asyncio
import logging

from flask import Flask, jsonify, request

from connector_service.providers.teams import TeamsNotificationError
from connector_service.providers.visio import VisioTaskError
from connector_service.providers.zoom import ZoomEventError
from connector_service.signatures import verify_zoom_signature, zoom_url_validation

logger = logging.getLogger(__name__)


def _run_handler(handler, payload, parse_error):
    try:
        result = asyncio.run(handler.handle(payload))
    except parse_error as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001 — échec fetch/ingest → 502, jamais un 500 opaque
        return jsonify({"error": "ingestion échouée"}), 502
    return jsonify({"job_id": result.job_id, "idempotent": result.idempotent}), 202


def register_visio_receiver(app: Flask, *, api_token: str, handler) -> None:
    @app.post("/api/v1/tasks/")
    def visio_task():
        if request.headers.get("Authorization", "") != f"Bearer {api_token}":
            return jsonify({"error": "non autorisé"}), 401
        payload = request.get_json(silent=True) or {}
        return _run_handler(handler, payload, VisioTaskError)


def register_zoom_receiver(app: Flask, *, secret_token: str, handler) -> None:
    @app.post("/webhooks/zoom")
    def zoom_webhook():
        raw = request.get_data(as_text=True)
        payload = request.get_json(silent=True) or {}
        # 1) Défi de validation d'URL (Zoom l'exige pour activer l'endpoint).
        if payload.get("event") == "endpoint.url_validation":
            plain = str((payload.get("payload") or {}).get("plainToken") or "")
            return jsonify(zoom_url_validation(secret_token, plain)), 200
        # 2) Authenticité : signature HMAC (rejette tout ce qui n'est pas signé par Zoom).
        ts = request.headers.get("x-zm-request-timestamp", "")
        sig = request.headers.get("x-zm-signature", "")
        if not verify_zoom_signature(secret_token, ts, raw, sig):
            return jsonify({"error": "signature Zoom invalide"}), 401
        return _run_handler(handler, payload, ZoomEventError)


def teams_notification_is_authentic(payload: dict, client_state: str) -> bool:
    """Le `clientState` reçu correspond-il à celui posé à la création de l'abonnement ?

    Fonction PURE, donc testée. Première barrière seulement : la vérification des
    `validationTokens` signés par Microsoft (cf. `graph_validation`) s'y ajoute quand les
    notifications riches sont activées.

    Un lot dont AUCUN élément ne porte de `clientState` est accepté : Graph n'en met pas dans
    les notifications de cycle de vie, et les refuser couperait le renouvellement.
    """
    if not client_state:
        return True                       # aucun secret configuré : rien à comparer
    states = {str((v or {}).get("clientState") or "")
              for v in (payload.get("value") or []) if isinstance(v, dict)}
    states.discard("")
    return not states or states == {client_state}


def register_teams_receiver(app: Flask, *, client_state: str, handler) -> None:
    @app.post("/webhooks/teams")
    def teams_webhook():
        # 1) Validation d'abonnement : Graph POSTe ?validationToken=… → on l'écho en text/plain.
        token = request.args.get("validationToken")
        if token is not None:
            return token, 200, {"Content-Type": "text/plain"}

        payload = request.get_json(silent=True) or {}

        # 2) RÈGLE MICROSOFT, contre-intuitive mais explicite : répondre « 202 Accepted »
        # immédiatement, MÊME quand la validation échoue — « accepting and responding to a
        # change notification prevents unnecessary delivery retries and HIDES VALIDATION
        # RESULTS FROM POTENTIAL ATTACKERS ».
        #
        # Renvoyer 401 comme le faisait la première version renseignait donc un attaquant sur
        # ce qui avait été détecté, et provoquait des réémissions inutiles. On accepte, puis
        # on ignore EN SILENCE côté client — en le journalisant pour nous.
        if not teams_notification_is_authentic(payload, client_state):
            logger.warning("notification Teams au clientState inattendu — acceptée puis "
                           "IGNORÉE (répondre 401 renseignerait un attaquant)")
            return jsonify({"accepted": True}), 202

        return _run_handler(handler, payload, TeamsNotificationError)

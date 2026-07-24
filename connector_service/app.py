"""Récepteur HTTP du connecteur Visio post-réunion (A1 — Flask, sync).

Reçoit la tâche `POST /api/v1/tasks/` que Visio POSTe (auth Bearer `app_api_token`), la
passe au `VisioIngestHandler` async (via `asyncio.run` — request/response ponctuel), qui
crée UN job TranscrIA idempotent. Flask (comme `inference_service`), pas FastAPI.

Le `handler` est INJECTÉ : la CI teste le récepteur avec un handler factice ; en
production `build_handler()` le construit depuis la config (transport requests réel +
fetcher MinIO réel + pont vers l'API de jobs).
"""
from __future__ import annotations

import asyncio

from flask import Flask, jsonify, request

from connector_service.bridge import JobsApiBridge
from connector_service.fetchers import MinioArtifactFetcher
from connector_service.providers.visio import VisioIngestHandler, VisioTaskAdapter, VisioTaskError
from connector_service.transports import RequestsTransport


def build_handler(config: dict) -> VisioIngestHandler:
    """Construit le handler Visio RÉEL depuis la config (transport requests + MinIO)."""
    return VisioIngestHandler(
        VisioTaskAdapter(bucket=config["minio_bucket"]),
        MinioArtifactFetcher(
            endpoint_url=config["minio_endpoint"],
            access_key=config["minio_access_key"],
            secret_key=config["minio_secret_key"],
        ),
        JobsApiBridge(config["transcria_base_url"], config["transcria_api_token"], RequestsTransport()),
    )


def create_connector_app(*, api_token: str, handler: VisioIngestHandler) -> Flask:
    """App Flask du connecteur. `api_token` = secret Bearer attendu de la plateforme."""
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.post("/api/v1/tasks/")
    def receive_task():
        if request.headers.get("Authorization", "") != f"Bearer {api_token}":
            return jsonify({"error": "non autorisé"}), 401
        payload = request.get_json(silent=True) or {}
        try:
            result = asyncio.run(handler.handle(payload))
        except VisioTaskError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:  # noqa: BLE001 — échec fetch/ingest → 502, jamais un 500 opaque
            return jsonify({"error": "ingestion échouée"}), 502
        return jsonify({"job_id": result.job_id, "idempotent": result.idempotent}), 202

    return app

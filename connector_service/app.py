"""Service connecteur — app Flask multi-plateforme, pilotée par CONFIG (A1-A3).

Monte les récepteurs des plateformes ACTIVÉES (Visio task / Zoom webhook / Teams
notification) autour d'un pont partagé vers l'API de jobs TranscrIA. Flask (comme
`inference_service`) ; lancé par `python -m connector_service` (dev) ou un serveur WSGI
(prod). Opt-in : une plateforme non activée n'a pas de route.

Les handlers sont construits depuis la config (`build_handlers`) ou INJECTÉS (tests).
"""
from __future__ import annotations

from flask import Flask, jsonify

from connector_service.bridge import JobsApiBridge
from connector_service.fetchers import HttpArtifactFetcher, MinioArtifactFetcher
from connector_service.ingest import PostMeetingIngestHandler, parse_teams, parse_zoom
from connector_service.providers.visio import VisioIngestHandler, VisioTaskAdapter
from connector_service.receivers import (
    register_teams_receiver,
    register_visio_receiver,
    register_zoom_receiver,
)
from connector_service.transports import RequestsTransport


def _bridge(config: dict) -> JobsApiBridge:
    return JobsApiBridge(config["transcria_base_url"], config["transcria_api_token"],
                         RequestsTransport())


def build_handlers(config: dict) -> dict:
    """Construit les handlers RÉELS des plateformes activées (`enabled: true`)."""
    bridge = _bridge(config)
    handlers: dict = {}
    visio = config.get("visio") or {}
    if visio.get("enabled"):
        handlers["visio"] = VisioIngestHandler(
            VisioTaskAdapter(bucket=visio["minio_bucket"]),
            MinioArtifactFetcher(endpoint_url=visio["minio_endpoint"],
                                 access_key=visio["minio_access_key"],
                                 secret_key=visio["minio_secret_key"]),
            bridge)
    zoom = config.get("zoom") or {}
    if zoom.get("enabled"):
        # Zoom : le download_token voyage dans l'artefact (HttpArtifactFetcher par défaut).
        handlers["zoom"] = PostMeetingIngestHandler("zoom", parse_zoom, HttpArtifactFetcher(), bridge)
    teams = config.get("teams") or {}
    if teams.get("enabled"):
        # Teams : jeton OAuth Graph (acquisition MSAL réelle = gate manuel ; ici statique).
        graph_token = str(teams.get("graph_token") or "")
        handlers["teams"] = PostMeetingIngestHandler(
            "teams", parse_teams, HttpArtifactFetcher(lambda art: graph_token), bridge)
    return handlers


def create_connector_app(config: dict, *, handlers: dict | None = None) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "platforms": sorted((handlers or {}).keys()
                                                            or _enabled(config))}), 200

    active = handlers if handlers is not None else build_handlers(config)
    if "visio" in active:
        register_visio_receiver(app, api_token=config["visio"]["api_token"],
                                handler=active["visio"])
    if "zoom" in active:
        register_zoom_receiver(app, secret_token=config["zoom"]["secret_token"],
                               handler=active["zoom"])
    if "teams" in active:
        register_teams_receiver(app, client_state=str((config.get("teams") or {}).get("client_state") or ""),
                                handler=active["teams"])
    return app


def _enabled(config: dict) -> list[str]:
    return [p for p in ("visio", "zoom", "teams") if (config.get(p) or {}).get("enabled")]

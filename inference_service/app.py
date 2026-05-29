"""Fabrique de l'application Flask du service d'inférence.

Choix Flask (et non FastAPI) : homogénéité avec le frontend TranscrIA (un seul
framework), zéro nouvelle dépendance, et l'async n'apporte rien à un service
GPU-bound dont chaque requête monopolise la carte.
"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify

from inference_service.diarize_engine import DiarizeEngine
from inference_service.engine import VoiceEmbedEngine
from inference_service.errors import InferenceError

logger = logging.getLogger("inference_service")


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    level = os.environ.get("INFERENCE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def create_app(
    config: dict | None = None,
    engine: VoiceEmbedEngine | None = None,
    diarize_engine: DiarizeEngine | None = None,
) -> Flask:
    """Crée l'app du service.

    Args:
        config: configuration TranscrIA. Si None, chargée via transcria.config.
        engine: moteur embedding injecté (tests). Sinon construit depuis la config.
        diarize_engine: moteur diarisation injecté (tests). Sinon depuis la config.
    """
    _configure_logging()
    app = Flask("transcria_inference")

    if config is None:
        from transcria.config import load_config
        config = load_config()
    app.config["TRANSCRIA_CONFIG"] = config

    # Moteurs résidents, partagés par toutes les requêtes (verrou interne par moteur → GPU sérialisé).
    app.extensions["voice_engine"] = engine or VoiceEmbedEngine(config)
    app.extensions["diarize_engine"] = diarize_engine or DiarizeEngine(config)

    from inference_service.routes.diarize import diarize_bp
    from inference_service.routes.health import health_bp
    from inference_service.routes.voice_embed import voice_embed_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(voice_embed_bp)
    app.register_blueprint(diarize_bp)

    _register_error_handlers(app)
    logger.info("TranscrIA Inference Service initialisé (voice-embed, diarize)")
    return app


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(InferenceError)
    def _handle_inference_error(exc: InferenceError):
        response = jsonify(exc.to_dict())
        response.status_code = exc.http_status
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.errorhandler(404)
    def _handle_404(_exc):
        return jsonify({"error": "not_found", "message": "Endpoint inconnu"}), 404

    @app.errorhandler(405)
    def _handle_405(_exc):
        return jsonify({"error": "method_not_allowed", "message": "Méthode non autorisée"}), 405

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        logger.exception("Erreur non gérée")
        return jsonify({"error": "internal_error", "message": str(exc)}), 500

"""Sondes de supervision : /health (process up), /ready (moteur servable), /models."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)


def _engines():
    ext = current_app.extensions
    return [ext["voice_engine"], ext["diarize_engine"]]


@health_bp.route("/health", methods=["GET"])
def health():
    """Le process répond — ne charge aucun modèle, toujours 200 si vivant."""
    return jsonify({"status": "ok", "service": "transcria-inference"}), 200


@health_bp.route("/ready", methods=["GET"])
def ready():
    """Prêt à servir : les moteurs existent et peuvent charger/servent déjà.

    Renvoie 200 même si les modèles ne sont pas encore chargés (CAS B =
    chargeable à la demande). Le déchargement idle est tenté ici, coût nul sinon.
    """
    engines = _engines()
    for engine in engines:
        engine.maybe_unload_if_idle()
    return jsonify({"status": "ready", "models": [e.status() for e in engines]}), 200


@health_bp.route("/models", methods=["GET"])
def models():
    """Inventaire des modèles servis et leur état (loaded/unloaded, device…)."""
    return jsonify({"models": [e.status() for e in _engines()]}), 200

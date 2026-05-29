"""Sondes de supervision : /health (process up), /ready (moteur servable), /models."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)


def _engine():
    return current_app.extensions["voice_engine"]


@health_bp.route("/health", methods=["GET"])
def health():
    """Le process répond — ne charge aucun modèle, toujours 200 si vivant."""
    return jsonify({"status": "ok", "service": "transcria-inference"}), 200


@health_bp.route("/ready", methods=["GET"])
def ready():
    """Prêt à servir : le moteur existe et peut charger/sert déjà le modèle.

    Renvoie 200 même si le modèle n'est pas encore chargé (CAS B = chargeable
    à la demande). Le déchargement idle est tenté ici, à coût nul si non requis.
    """
    engine = _engine()
    engine.maybe_unload_if_idle()
    return jsonify({"status": "ready", "model": engine.status()}), 200


@health_bp.route("/models", methods=["GET"])
def models():
    """Inventaire des modèles servis et leur état (loaded/unloaded, device…)."""
    engine = _engine()
    return jsonify({"models": [engine.status()]}), 200

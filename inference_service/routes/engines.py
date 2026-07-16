"""POST /engines/ensure — lance/réutilise un moteur STT déclaré (cycle de vie A/B/C).

Active l'autonomie du nœud (docs/SERVICE_RESSOURCES_GPU.md §2.2) : le nœud possède
les scripts, donc il lance ses propres moteurs à la demande — la frontale demande
juste « assure le moteur X » (topology-agnostique : 127.0.0.1 ou distant).

Endpoint de CONTRÔLE → protégé par clé API (comme /infer/*). Réponses :
  200 ready/launched | 503 busy (+Retry-After) | 404 moteur inconnu | 502 échec.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from transcria.gpu.stt_engine_supervisor import build_stt_supervisor, engine_specs_from_config

logger = logging.getLogger("inference_service.engines")

engines_bp = Blueprint("engines", __name__)

_HTTP_BY_STATUS = {"ready": 200, "launched": 200, "busy": 503, "error": 502}


@engines_bp.route("/engines/ensure", methods=["POST"])
def ensure_engine():
    config = current_app.config["TRANSCRIA_CONFIG"]
    body = request.get_json(silent=True) or {}
    name = str(body.get("engine") or "")

    specs = {s.name: s for s in engine_specs_from_config(config)}
    spec = specs.get(name)
    if spec is None:
        return jsonify({
            "error": "unknown_engine",
            "message": f"moteur '{name}' non déclaré dans resource_node.engines",
            "available": sorted(specs),
        }), 404

    supervisor = current_app.extensions.get("stt_supervisor") or build_stt_supervisor(config)
    result = supervisor.ensure_ready(spec)
    logger.info("ensure_engine %s → %s (gpu=%s) : %s", name, result.status, result.gpu_index, result.reason)

    resp = jsonify({
        "engine": name,
        "status": result.status,
        "gpu_index": result.gpu_index,
        "reason": result.reason,
    })
    resp.status_code = _HTTP_BY_STATUS.get(result.status, 500)
    if result.status == "busy":
        resp.headers["Retry-After"] = "30"   # CAS C : la frontale re-queue
    return resp

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

from transcria.gpu.stt_engine_supervisor import (
    build_stt_supervisor,
    engine_specs_from_config,
    specs_for_backend,
)

logger = logging.getLogger("inference_service.engines")

engines_bp = Blueprint("engines", __name__)

_HTTP_BY_STATUS = {"ready": 200, "launched": 200, "busy": 503, "error": 502}


@engines_bp.route("/engines/ensure", methods=["POST"])
def ensure_engine():
    config = current_app.config["TRANSCRIA_CONFIG"]
    body = request.get_json(silent=True) or {}
    name = str(body.get("engine") or "")

    all_specs = engine_specs_from_config(config)
    # Multi-instance (§2.9) : « assure le moteur X » couvre toutes les entrées
    # servant le backend X (nom exact ou champ `backend`). La frontale ignore le
    # manifeste du nœud — c'est ICI que le nom logique se déplie en instances.
    matched = specs_for_backend(all_specs, name)
    if not matched:
        return jsonify({
            "error": "unknown_engine",
            "message": f"moteur '{name}' non déclaré dans resource_node.engines",
            "available": sorted(s.name for s in all_specs),
        }), 404

    supervisor = current_app.extensions.get("stt_supervisor") or build_stt_supervisor(config)
    # La PREMIÈRE instance porte le verdict (contrat historique 1 moteur = 1 statut) ;
    # les instances supplémentaires sont best-effort : une panne dégrade le débit,
    # pas le job (le pool client de la frontale bascule sur les vivantes).
    result = supervisor.ensure_ready(matched[0])
    logger.info("ensure_engine %s → %s (gpu=%s) : %s", matched[0].name, result.status,
                result.gpu_index, result.reason)
    extra_statuses: list[dict] = []
    if result.ok:
        for spec in matched[1:]:
            try:
                extra = supervisor.ensure_ready(spec)
            except Exception as exc:  # noqa: BLE001 — best-effort, jamais bloquant
                logger.warning("ensure_engine instance secondaire %s : %s — poursuite", spec.name, exc)
                extra_statuses.append({"engine": spec.name, "status": "error", "reason": str(exc)})
                continue
            logger.info("ensure_engine %s → %s (gpu=%s) : %s", spec.name, extra.status,
                        extra.gpu_index, extra.reason)
            extra_statuses.append({"engine": spec.name, "status": extra.status,
                                   "gpu_index": extra.gpu_index, "reason": extra.reason})

    payload = {
        "engine": matched[0].name,
        "status": result.status,
        "gpu_index": result.gpu_index,
        "reason": result.reason,
    }
    if extra_statuses:  # champ ADDITIF : absent en mono-instance (contrat historique)
        payload["instances"] = extra_statuses
    resp = jsonify(payload)
    resp.status_code = _HTTP_BY_STATUS.get(result.status, 500)
    if result.status == "busy":
        resp.headers["Retry-After"] = "30"   # CAS C : la frontale re-queue
    return resp

"""GET /capabilities — inventaire des ressources du nœud (libre, supervision).

Comme /health, cet endpoint n'exige pas de clé API : il sert au panneau d'état de
la frontale (mode de déploiement, feu vert par moteur, VRAM). Cf.
docs/SERVICE_RESSOURCES_GPU.md §6/§7.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from inference_service.capabilities import build_capabilities

capabilities_bp = Blueprint("capabilities", __name__)


@capabilities_bp.route("/capabilities", methods=["GET"])
def capabilities():
    config = current_app.config["TRANSCRIA_CONFIG"]
    ext = current_app.extensions

    from transcria.gpu.stt_engine_supervisor import engine_specs_from_config, http_health_prober
    from transcria.gpu.stt_vram_planner import gpu_states_from_vram_manager
    from transcria.gpu.vram_manager import VRAMManager

    gpu_states = gpu_states_from_vram_manager(VRAMManager(config=config))
    inprocess = [ext["voice_engine"].status(), ext["diarize_engine"].status()]
    stt_specs = engine_specs_from_config(config)

    # Idle-stop opportuniste : ce poll (panneau frontale ~10 s) sert de battement
    # pour réclamer les moteurs inactifs (best-effort, sans tâche de fond).
    supervisor = ext.get("stt_supervisor")
    if supervisor is not None:
        try:
            supervisor.reap_idle(stt_specs)
        except Exception:  # noqa: BLE001 — best-effort, ne bloque jamais l'inventaire
            pass

    payload = build_capabilities(
        config,
        gpu_states=gpu_states,
        inprocess_statuses=inprocess,
        stt_specs=stt_specs,
        health_prober=lambda url: http_health_prober(url, timeout=2.0),
    )
    return jsonify(payload), 200

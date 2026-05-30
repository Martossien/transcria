"""Statut des ressources distantes & politique d'admission en mode dégradé.

Côté frontale (docs/SERVICE_RESSOURCES_GPU.md §7). Fonctions **pures** :

  - `remote_requirements(config)` : quelles capacités ce job exige à distance.
  - `assess_admission(...)` : admettre / mettre en file / échouer, selon que le nœud
    est joignable et depuis combien de temps (jamais d'échec silencieux ni de spin).
  - `summarize_capabilities(...)` : forme prête à afficher (panneau d'état frontale).

La politique d'admission se base sur la **joignabilité du nœud**, pas sur l'état
up/down de chaque moteur : un moteur STT éteint sera lancé à la demande par le
superviseur côté nœud (CAS B). L'état par moteur reste informatif (panneau).
"""
from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_MAX_UNAVAILABLE_S = 600


def remote_requirements(config: dict) -> set[str]:
    """Capacités servies à distance pour cette config : sous-ensemble de
    {"stt", "diarize", "voice_embed"}. Vide = tout local."""
    reqs: set[str] = set()
    inf = config.get("inference", {}) or {}
    mode = inf.get("mode", "local")

    from transcria.stt.transcriber_factory import _should_use_remote_stt

    backend = config.get("models", {}).get("stt_backend", "cohere")
    if _should_use_remote_stt(config, backend):
        reqs.add("stt")
    if config.get("models", {}).get("diarization_backend") == "remote":
        reqs.add("diarize")
    if mode in ("remote", "hybrid") and (inf.get("url") or inf.get("base_url")):
        reqs.add("voice_embed")
    return reqs


@dataclass(frozen=True)
class AdmissionVerdict:
    """Décision d'admission d'un job vis-à-vis des ressources distantes.

    action : "admit" (lancer) | "queue" (file, indispo transitoire) | "fail"
    (échec explicite, fenêtre dépassée).
    """

    action: str
    reason: str


def assess_admission(
    config: dict,
    *,
    reachable: bool,
    unavailable_for_s: float = 0.0,
) -> AdmissionVerdict:
    """Politique §7.2. `max_unavailable_s` est lu dans inference.resilience."""
    if not remote_requirements(config):
        return AdmissionVerdict("admit", "tout local — aucune ressource distante requise")
    if reachable:
        return AdmissionVerdict("admit", "ressources distantes joignables")

    inf = config.get("inference", {}) or {}
    max_unavailable_s = float(
        (inf.get("resilience", {}) or {}).get("max_unavailable_s", _DEFAULT_MAX_UNAVAILABLE_S)
    )
    if unavailable_for_s >= max_unavailable_s:
        return AdmissionVerdict(
            "fail",
            f"ressources distantes injoignables depuis {unavailable_for_s:.0f}s "
            f"(> {max_unavailable_s:.0f}s)",
        )
    return AdmissionVerdict(
        "queue",
        f"ressources distantes injoignables (transitoire, {unavailable_for_s:.0f}s) — mis en file",
    )


def summarize_capabilities(capabilities: dict | None) -> dict:
    """Forme prête à afficher dans le panneau d'état de la frontale.

    `None` = nœud injoignable. Les moteurs in-process sont considérés disponibles
    dès que le service répond (chargement à la demande, CAS B).
    """
    if not capabilities:
        return {"reachable": False, "mode": None, "gpus": [], "engines": []}

    engines: list[dict] = []
    for e in capabilities.get("stt_engines", []) or []:
        engines.append({"name": e.get("name"), "kind": "stt", "up": bool(e.get("up"))})
    for s in capabilities.get("inprocess", []) or []:
        engines.append({"name": s.get("name"), "kind": "inprocess", "up": True})

    return {
        "reachable": True,
        "mode": capabilities.get("deployment_mode"),
        "gpus": capabilities.get("gpus", []),
        "engines": engines,
    }

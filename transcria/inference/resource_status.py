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

from transcria.stt.transcriber_factory import _should_use_remote_stt, summary_backend

_DEFAULT_MAX_UNAVAILABLE_S = 600


def remote_requirements(config: dict) -> set[str]:
    """Capacités servies à distance pour cette config : sous-ensemble de
    {"stt", "diarize", "voice_embed"}. Vide = tout local."""
    reqs: set[str] = set()
    inf = config.get("inference", {}) or {}
    mode = inf.get("mode", "local")

    # Le backend PRINCIPAL et celui de la PHASE RÉSUMÉ (lot 2) peuvent différer :
    # l'un ou l'autre servi à distance suffit à exiger la capacité « stt ».
    backend = config.get("models", {}).get("stt_backend", "cohere")
    if _should_use_remote_stt(config, backend) or _should_use_remote_stt(config, summary_backend(config)):
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
        item = {
            "name": e.get("name"),
            "kind": "stt",
            "up": bool(e.get("up")),
        }
        for key in ("ensure_in_progress", "last_used_monotonic_s"):
            if key in e:
                item[key] = e.get(key)
        engines.append(item)
    for s in capabilities.get("inprocess", []) or []:
        item = {"name": s.get("name"), "kind": "inprocess", "up": True}
        for key in ("loaded", "capacity", "inflight", "queued", "busy", "last_wait_s"):
            if key in s:
                item[key] = s.get(key)
        engines.append(item)

    return {
        "reachable": True,
        "mode": capabilities.get("deployment_mode"),
        "gpus": capabilities.get("gpus", []),
        "engines": engines,
    }


def available_remote_slots(config: dict, capabilities: dict | None) -> int | None:
    """Capacité de dispatch distante actuellement exploitable.

    Retourne :
      - `None` si la config ne requiert pas de ressource distante, ou si
        `/capabilities` ne contient pas assez d'information fiable pour borner ;
      - un entier >= 0 si le nœud expose une capacité exploitable.

    Cette valeur est une optimisation de backpressure scheduler. Elle ne remplace
    pas le pré-vol `resource_gate`, qui reste l'autorité pour gérer les erreurs
    réseau, le mode dégradé et l'auto-lancement des moteurs.
    """
    reqs = remote_requirements(config)
    if not reqs or not capabilities:
        return None

    slots: list[int] = []
    # Capacité d'ADMISSION du nœud (resource_node.max_concurrent_jobs, défaut 1 = séquentiel) :
    # borne supérieure du nombre de pipelines concurrents lancés contre ce nœud.
    node_max = capabilities.get("max_concurrent_jobs")
    if isinstance(node_max, int) and node_max >= 0:
        slots.append(node_max)
    if "stt" in reqs:
        stt_slots = _stt_slots(config, capabilities)
        if stt_slots is not None:
            slots.append(stt_slots)
    # Les moteurs in-process sérialisés (diarize/voice-embed) NE bornent PLUS l'admission : ils
    # s'auto-sérialisent via leur verrou moteur (les jobs en surplus y font la queue, comptés
    # `queued`), tandis que STT/LLM (vLLM) batchent. Leur mono-capacité (1) plafonnait à tort le
    # dispatch à 1 dès qu'UNE diarisation tournait (capacity−inflight−queued → 0). Le plafond
    # réel d'admission est désormais `max_concurrent_jobs`. Le pré-vol resource_gate reste l'autorité
    # (nœud joignable, ensure STT, mode dégradé).

    return min(slots) if slots else None


def remote_vram_admits(config: dict, capabilities: dict | None, vram_profile: dict | None) -> bool | None:
    """Admission VRAM distante depuis `/capabilities`.

    `True`  : le coût distant connu tient sur au moins un GPU du nœud.
    `False` : le coût distant connu ne tient nulle part.
    `None`  : pas de besoin distant ou données insuffisantes, laisser le pré-vol
              existant décider.
    """
    reqs = remote_requirements(config)
    if not reqs or not capabilities or not isinstance(vram_profile, dict):
        return None
    required_mb = _remote_required_mb(reqs, vram_profile)
    if required_mb <= 0:
        return None
    headroom_mb = int((config.get("gpu", {}) or {}).get("min_free_vram_mb", 4000))
    saw_usable = False
    for gpu in capabilities.get("gpus", []) or []:
        if not isinstance(gpu, dict):
            continue
        raw = gpu.get("free_mb")
        if raw is None:
            continue  # free_mb absent → donnée GPU inexploitable pour ce GPU
        try:
            free_mb = int(raw)
        except (TypeError, ValueError):
            continue  # free_mb illisible → idem
        saw_usable = True
        if free_mb >= required_mb + headroom_mb:
            return True
    # Aucune donnée GPU exploitable (liste vide OU aucun free_mb lisible) = données
    # insuffisantes → None : laisser le pré-vol (resource_gate, l'autorité) décider.
    # Retourner False ici BLOQUERAIT le dispatch en permanence (famine) si le nœud
    # n'énumère pas ses GPU. False seulement quand on a vu des GPU et qu'aucun ne tient.
    return False if saw_usable else None


def remote_gpu_data_missing(config: dict, capabilities: dict | None) -> bool:
    """True si le job requiert du distant, le nœud est joignable (capabilities présentes)
    mais n'expose AUCUN GPU avec un `free_mb` exploitable.

    Prédicat **pur** de diagnostic : le scheduler s'en sert pour émettre un WARNING
    throttlé (le dispatch défère alors au pré-vol via `remote_vram_admits` → None).
    """
    if not remote_requirements(config) or not capabilities:
        return False
    for gpu in capabilities.get("gpus", []) or []:
        if not isinstance(gpu, dict):
            continue
        raw = gpu.get("free_mb")
        if raw is None:
            continue
        try:
            int(raw)
        except (TypeError, ValueError):
            continue
        return False  # au moins un GPU avec donnée exploitable
    return True


def _remote_required_mb(reqs: set[str], vram_profile: dict) -> int:
    phases = vram_profile.get("phases")
    if isinstance(phases, dict):
        remote_phases: set[str] = set()
        if "stt" in reqs:
            remote_phases.update({"stt", "summary_stt"})
        if "diarize" in reqs:
            remote_phases.add("diarization")
        if "voice_embed" in reqs:
            remote_phases.add("voice_embed")
        values = [_positive_int(phases.get(name)) for name in remote_phases]
        return max(values, default=0)
    return _positive_int(vram_profile.get("peak_vram_mb"))


def _stt_slots(config: dict, capabilities: dict) -> int | None:
    backend = config.get("models", {}).get("stt_backend", "cohere")
    engine = next(
        (item for item in capabilities.get("stt_engines", []) or [] if item.get("name") == backend),
        None,
    )
    if not engine:
        return None
    if engine.get("ensure_in_progress"):
        return 0
    stt_cfg = (config.get("inference", {}) or {}).get("stt", {}) or {}
    try:
        configured = int(stt_cfg.get("concurrency", 1))
    except (TypeError, ValueError):
        configured = 1
    configured = max(1, configured)
    # Moteur éteint mais déclaré : le premier job peut déclencher ensure(CAS C),
    # les autres attendront un tick suivant au lieu de se bousculer sur ensure.
    return configured if engine.get("up") else 1


def _positive_int(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)

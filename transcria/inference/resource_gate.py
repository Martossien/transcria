"""Pré-vol des ressources distantes avant d'exécuter un job (étapes 1 + 2).

Combine, en une décision pure et testable :
  - **admission** (§7.2) : nœud joignable ? sinon file (transitoire) ou échec
    (au-delà de `inference.resilience.max_unavailable_s`) ;
  - **auto-lancement STT** (CAS B) : si admis, demande au nœud d'assurer le moteur
    STT via /engines/ensure (le nœud lance à la demande, non intrusif).

Renvoie `GateVerdict(action ∈ {proceed, fail, defer})` + `unavailable_since` à
persister (suivi de la durée d'indisponibilité entre tentatives). Sans nœud de
contrôle (pas d'`inference.url`, ex. STT vLLM direct), on laisse la résilience au
niveau requête (503/retry/fallback) faire le travail → proceed.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from transcria.gpu.stt_engine_supervisor import build_stt_supervisor, engine_specs_from_config
from transcria.inference.client import (
    InferenceClient,
    InferenceRequestError,
    InferenceUnavailable,
    build_client_from_config,
)
from transcria.inference.resource_status import assess_admission, remote_requirements
from transcria.stt.transcriber_factory import _should_use_remote_stt, summary_backend

logger = logging.getLogger(__name__)

_DEFAULT_RETRY_AFTER_S = 30


@dataclass(frozen=True)
class GateVerdict:
    action: str                       # "proceed" | "fail" | "defer"
    reason: str
    retry_after_s: int = 0
    unavailable_since: float | None = None   # à persister (suivi inter-tentatives)


def _probe_reachable(client: InferenceClient) -> bool:
    """Le nœud répond-il ? /capabilities OK ou 4xx = joignable ; réseau/5xx = non."""
    try:
        client.capabilities()
        return True
    except InferenceUnavailable:
        return False
    except InferenceRequestError:
        return True   # le service répond (4xx) → joignable


def _stt_loopback_backends(config: dict) -> "list[str]":
    """Backends STT routés vers une URL loopback, parmi le PRINCIPAL et celui de la
    PHASE RÉSUMÉ (lot 2 — ils peuvent différer : ex. cohere natif + qwen3asr servi
    pour le résumé). Détection par urlparse, sans résolution DNS."""
    from urllib.parse import urlparse

    models = config.get("models", {})
    candidates = [str(models.get("stt_backend", "cohere")), str(summary_backend(config))]
    backends_cfg = (((config.get("inference", {}) or {}).get("stt", {}) or {})
                    .get("backends", {}) or {})
    loopback: list[str] = []
    for backend in dict.fromkeys(candidates):  # dédupliqué, ordre stable
        url = str((backends_cfg.get(backend, {}) or {}).get("url") or "")
        if url and (urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1"):
            loopback.append(backend)
    return loopback


def _ensure_local_served_stt(config: dict, *, supervisor_factory=None) -> "GateVerdict | None":
    """Assure EN PROCESS un moteur STT servi localement (all-in-one).

    Ne s'active QUE si l'URL du backend pointe loopback ET qu'un moteur homonyme est
    déclaré dans `resource_node.engines` — sinon None (comportement historique).
    busy/error → defer (transitoire, jamais fail dur) ; ready/launched → proceed."""
    backends = _stt_loopback_backends(config)
    if not backends:
        return None

    specs = {s.name: s for s in engine_specs_from_config(config)}
    factory = supervisor_factory or build_stt_supervisor
    statuses: list[str] = []
    for backend in backends:
        spec = specs.get(backend)
        if spec is None:
            continue  # loopback sans moteur déclaré : résilience au niveau requête
        try:
            result = factory(config).ensure_ready(spec)
        except Exception as exc:  # noqa: BLE001 — le gate ne doit jamais faire échouer un job
            logger.warning("[gate] ensure local du moteur '%s' impossible (%s) — defer", backend, exc)
            return GateVerdict("defer", f"moteur servi local '{backend}' : {exc}",
                               retry_after_s=_DEFAULT_RETRY_AFTER_S)
        if not result.ok:
            logger.warning("[gate] moteur servi local '%s' %s : %s", backend, result.status, result.reason)
            return GateVerdict("defer", f"moteur servi local '{backend}' {result.status} : {result.reason}",
                               retry_after_s=_DEFAULT_RETRY_AFTER_S)
        statuses.append(f"'{backend}' {result.status} (GPU {result.gpu_index})")
    if not statuses:
        return None
    return GateVerdict("proceed", "moteur(s) servi(s) local(aux) " + ", ".join(statuses))


def prepare_remote_resources(
    config: dict,
    *,
    unavailable_since: float | None = None,
    now: float | None = None,
    client_factory: Callable[[dict], "InferenceClient | None"] | None = None,
    supervisor_factory=None,
) -> GateVerdict:
    now = now if now is not None else time.time()
    reqs = remote_requirements(config)
    if not reqs:
        return GateVerdict("proceed", "tout local — rien à préparer")

    factory = client_factory or build_client_from_config
    client = factory(config)
    if client is None:
        # Ressources distantes sans nœud de contrôle. Deux sous-cas :
        # — moteur STT SERVI LOCALEMENT (all-in-one : URL loopback + moteur homonyme
        #   déclaré dans resource_node.engines) → on l'assure NOUS-MÊMES, en process
        #   (même cycle A/B/C que /engines/ensure côté nœud) ;
        # — sinon (ex. STT vLLM direct distant) : la résilience au niveau requête
        #   (503/retry/fallback) prend le relais → proceed historique.
        if "stt" in reqs:
            verdict = _ensure_local_served_stt(config, supervisor_factory=supervisor_factory)
            if verdict is not None:
                return verdict
        return GateVerdict("proceed", "pas de nœud de contrôle — résilience au niveau requête")

    reachable = _probe_reachable(client)
    if reachable:
        new_since, elapsed = None, 0.0
    else:
        new_since = unavailable_since if unavailable_since is not None else now
        elapsed = now - new_since

    admission = assess_admission(config, reachable=reachable, unavailable_for_s=elapsed)
    if admission.action == "fail":
        logger.error("Pré-vol ressources : échec — %s", admission.reason)
        return GateVerdict("fail", admission.reason, unavailable_since=new_since)
    if admission.action == "queue":
        logger.warning("Pré-vol ressources : mise en file — %s", admission.reason)
        return GateVerdict("defer", admission.reason, _DEFAULT_RETRY_AFTER_S, new_since)

    # Admis : assurer le(s) moteur(s) STT à la demande (CAS B) si STT est distant —
    # backend principal ET backend du résumé s'il diffère (lot 2).
    if "stt" in reqs:
        models = config.get("models", {})
        candidates = [str(models.get("stt_backend", "cohere")), str(summary_backend(config))]
        backend = ""
        try:
            for backend in dict.fromkeys(candidates):
                if not _should_use_remote_stt(config, backend):
                    continue
                res = client.ensure_engine(backend)
                logger.info("Pré-vol ressources : moteur STT '%s' → %s (gpu=%s)",
                            backend, res.get("status"), res.get("gpu_index"))
        except InferenceUnavailable as exc:
            # 503 busy (CAS C) ou injoignable juste après le probe → on diffère.
            logger.warning("Pré-vol ressources : moteur STT '%s' indisponible — file (%s)", backend, exc)
            return GateVerdict("defer", f"moteur STT en préparation: {exc}",
                               _DEFAULT_RETRY_AFTER_S, None)
        except InferenceRequestError as exc:
            # 404 (moteur non déclaré) etc. : on laisse la requête réelle trancher.
            logger.info("Pré-vol ressources : ensure '%s' non concluant (%s) — on poursuit", backend, exc)

    return GateVerdict("proceed", "ressources distantes prêtes", unavailable_since=None)

"""API JSON de traitement : lancement/annulation, polling de statut, relance,
qualité, export, état des ressources distantes et statut système.

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``, y compris le
cache court du panneau ressources (verrou et structure déménagent ENSEMBLE, §5.5).
"""
import copy
import logging
import threading
import time
from datetime import datetime

from flask import jsonify, request
from flask_login import login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, requires
from transcria.config import get_config
from transcria.diagnostics.system_status import get_system_status

# Accès PAR MODULE : les tests substituent build_client_from_config à la source.
from transcria.inference import client as inference_client
from transcria.inference.client import InferenceUnavailable
from transcria.inference.resource_status import remote_requirements, summarize_capabilities
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QueueStore
from transcria.queue.wait_estimate import queue_wait_estimates
from transcria.services.job_executor import get_job_executor
from transcria.services.pipeline_service import PipelineService
from transcria.web.blueprint import web_bp
from transcria.web.job_access import can_manage_queue_job, get_job_for_api
from transcria.web.request_helpers import api_stable
from transcria.workflow import profiles
from transcria.workflow.concurrency_profile import summarize_concurrency
from transcria.workflow.profiles import profile_for_job
from transcria.workflow.progress import get_workflow_progress
from transcria.workflow.resume import reset_resume_state
from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.timing_service import estimate_remaining
from transcria.workflow.transitions import (
    can_start_profile,
    get_execution_status,
    is_execution_active,
    mark_execution_cancelled,
    request_execution_cancel,
)

logger = logging.getLogger(__name__)

_RESOURCE_STATUS_CACHE_LOCK = threading.Lock()
_RESOURCE_STATUS_CACHE: dict[tuple, dict] = {}
_DEFAULT_RESOURCE_STATUS_CACHE_TTL_S = 5.0


def _resource_status_cache_ttl_s(cfg: dict) -> float:
    raw = ((cfg.get("inference", {}) or {}).get("resilience", {}) or {}).get(
        "capabilities_cache_ttl_s",
        _DEFAULT_RESOURCE_STATUS_CACHE_TTL_S,
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RESOURCE_STATUS_CACHE_TTL_S


def _resource_status_cache_key(cfg: dict, requirements: set[str]) -> tuple:
    inference = cfg.get("inference", {}) or {}
    models = cfg.get("models", {}) or {}
    stt = inference.get("stt", {}) or {}
    return (
        inference.get("mode", "local"),
        inference.get("url") or inference.get("base_url") or "",
        models.get("stt_backend", "cohere"),
        models.get("diarization_backend", "pyannote"),
        tuple(sorted(requirements)),
        repr(stt.get("backends", {})),
    )


def _get_cached_resource_status(key: tuple, now_s: float) -> dict | None:
    with _RESOURCE_STATUS_CACHE_LOCK:
        cached = _RESOURCE_STATUS_CACHE.get(key)
        if not cached:
            return None
        if float(cached["expires_at"]) <= now_s:
            _RESOURCE_STATUS_CACHE.pop(key, None)
            return None
        summary = copy.deepcopy(cached["summary"])
    summary["cached"] = True
    return summary


def _set_cached_resource_status(key: tuple, summary: dict, ttl_s: float, now_s: float) -> None:
    if ttl_s <= 0:
        return
    with _RESOURCE_STATUS_CACHE_LOCK:
        _RESOURCE_STATUS_CACHE[key] = {
            "expires_at": now_s + ttl_s,
            "summary": copy.deepcopy(summary),
        }


def _clear_resource_status_cache() -> None:
    """Réservé aux tests et aux changements explicites de config."""
    with _RESOURCE_STATUS_CACHE_LOCK:
        _RESOURCE_STATUS_CACHE.clear()


@web_bp.route("/api/resources/status", methods=["GET"])
@login_required
def api_resources_status():
    """État des ressources distantes pour le panneau frontale (mode dégradé inclus).

    Interroge /capabilities du nœud ; injoignable → reachable=False (la frontale
    affiche rouge, l'admission bascule en file/échec selon §7.2). Voir
    docs/SERVICE_RESSOURCES_GPU.md §7. Cache court par process pour éviter que
    chaque client web martèle directement le nœud de ressources.
    """
    cfg = get_config()
    requirements = remote_requirements(cfg)
    cache_key = _resource_status_cache_key(cfg, requirements)
    ttl_s = _resource_status_cache_ttl_s(cfg)
    now_s = time.monotonic()
    cached = _get_cached_resource_status(cache_key, now_s)
    if cached is not None:
        return jsonify(cached)

    client = inference_client.build_client_from_config(cfg)
    caps = None
    if client is not None:
        try:
            caps = client.capabilities()
        except InferenceUnavailable as exc:
            logger.info("Panneau ressources : nœud injoignable — %s", exc)
            caps = None
    summary = summarize_capabilities(caps)
    summary["requires_remote"] = sorted(requirements)
    # Profil de concurrence & goulot (C7/B8) : mesure best-effort côté frontale (c'est
    # l'orchestrateur qui exécute le workflow et connaît les durées par étape).
    try:
        queue_depth = QueueStore.count_by_status().get("waiting", 0)
    except Exception as exc:  # noqa: BLE001 — observabilité non bloquante
        logger.debug("Profondeur de file indisponible pour le profil de concurrence: %s", exc)
        queue_depth = 0
    summary["concurrency"] = summarize_concurrency(cfg, queue_depth=queue_depth)
    summary["cached"] = False
    _set_cached_resource_status(cache_key, summary, ttl_s, now_s)
    return jsonify(summary)


@web_bp.route("/api/jobs/<job_id>/process", methods=["POST"])
@login_required
@api_stable
def api_process(job_id: str):
    """Lance le traitement complet du job (mise en file — contrat scriptable)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    payload = request.get_json(silent=True) or {} if request.is_json else {}
    mode = payload.get("mode") or request.args.get("mode", "fast")
    if mode == "cancel":
        request_execution_cancel(job.id)
        if not is_execution_active(job) or get_execution_status(job) == "queued":
            QueueStore.dequeue(job.id, status="cancelled")
            mark_execution_cancelled(job.id)
            JobStore.update_state(job.id, JobState.CANCELLED)
            return jsonify({"status": "cancelled"})
        return jsonify({"status": "cancel_requested"})

    processing_profile_id = payload.get("processing_profile_id") or request.args.get("processing_profile_id")
    try:
        # `mode` (legacy fast/quality) reste accepté ; un `processing_profile_id` explicite a
        # priorité. Le 2e membre est le mode d'exécution legacy de routage (Phase 4 le supprimera).
        profile, mode = profiles.resolve_request(processing_profile_id, mode)
    except (KeyError, ValueError):
        return jsonify({"error": f"Profil/mode de traitement invalide: {processing_profile_id or mode}"}), 400

    if mode == "quality" and not cfg.get("workflow", {}).get("enable_quality_mode", True):
        return jsonify({"error": "Le mode qualité est désactivé par la configuration"}), 400

    if not can_start_profile(job.state, profile):
        return jsonify(
            {
                "error": "Le job n'est pas prêt pour ce profil de traitement",
                "current_state": job.state,
                "processing_profile_id": profile.id,
            }
        ), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": get_execution_status(job)}), 409

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    priority = payload.get("priority", request.args.get("priority"))
    scheduled_at = None
    scheduled_at_raw = payload.get("scheduled_at") or request.args.get("scheduled_at")
    if scheduled_at_raw:
        try:
            scheduled_at = datetime.fromisoformat(str(scheduled_at_raw).replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "scheduled_at: format ISO 8601 invalide"}), 400

    if priority is not None and not can_manage_queue_job(job):
        priority = None

    # Re-soumission utilisateur (ou nouveau run) : repartir d'un état de reprise PROPRE.
    # Les re-queues AUTOMATIQUES (vram_wait/deferred) préservent `completed_phases` — c'est
    # eux qui permettent la reprise ; ici c'est une intention utilisateur de (re)lancer.
    reset_resume_state(JobStore, job.id)

    vram_profile = PipelineService.estimate_profile_resources(cfg, profile)
    # Durée audio portée par l'entrée de file (DB) : la page File (non job-scoped) n'a pas
    # accès aux fichiers du job en mode frontale/nœud GPU — sans ça, l'estimation d'attente
    # serait vide en split. Cf. revue macro split.
    try:
        _aa = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).load_json("metadata/audio_analysis.json") or {}
        vram_profile["audio_seconds"] = float(_aa.get("duration_seconds") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort, l'attente retombe sur le fichier sinon
        pass
    try:
        result = executor.submit_process(
            job.id,
            str(audio_path),
            mode,
            priority=priority,
            scheduled_at=scheduled_at,
            vram_profile=vram_profile,
            processing_profile_id=profile.id,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": "active"}), 409
    JobStore.update(job.id, processing_mode=mode)
    if job.state != JobState.READY_TO_PROCESS.value:
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
    audit_log(
        action=AuditAction.JOB_ENQUEUE,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            # `processing_profile_id` = contrat produit ; `queue_mode`/`legacy_mode` = unité
            # d'exécution. On garde `mode` (= legacy) pour la compatibilité des consommateurs d'audit.
            "processing_profile_id": profile.id,
            "queue_mode": mode,
            "legacy_mode": mode,
            "mode": mode,
            "priority": result.get("priority"),
            "position": result.get("position"),
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        },
    )
    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "processing_profile_id": profile.id,
        "state": JobState.READY_TO_PROCESS.value,
        "execution_status": "queued",
        "queue_position": result.get("position"),
    }), 202


@web_bp.route("/api/jobs/<job_id>/status", methods=["GET"])
@login_required
@api_stable
def api_job_status(job_id: str):
    """Endpoint léger de polling — état courant du job pendant le traitement (contrat scriptable)."""
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    progress = get_workflow_progress(job)
    payload = {
        "state": job.state,
        "execution_status": get_execution_status(job) if is_execution_active(job) else "idle",
        "progress": progress,
        "eta": _live_eta(job, progress),
    }
    # Champs ADDITIFS du contrat ⭐ (0.3.8) : présents seulement quand le job attend
    # son tour en file — position 1-based et estimation calibrée machine (mêmes
    # calculs que /admin/queue, agrégat sans détail des jobs des autres).
    queue_info = _queue_wait_info(job)
    if queue_info:
        payload.update(queue_info)
    return jsonify(payload)


def _queue_wait_info(job) -> dict | None:
    try:
        position = QueueStore.get_position(job.id)
        if position is None:
            return None
        entries = QueueStore.get_ordered_queue(limit=10000, include_running=True)
        estimate = queue_wait_estimates(get_config(), entries).get(job.id)
        info: dict = {"queue_position": position}
        if estimate:
            info["wait_estimate"] = estimate
        return info
    except Exception:  # noqa: BLE001 — enrichissement best-effort, jamais bloquant
        return None


def _live_eta(job, progress) -> dict | None:
    """ETA live du traitement (temps restant calibré machine) pour le polling de suivi.
    None si l'estimation n'est pas pertinente (pas en traitement, pas de progression)."""
    if not isinstance(progress, dict) or progress.get("step") != "processing":
        return None
    try:
        profile = profile_for_job(job)
        if profile is None:
            return None
        cfg = get_config()
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        audio_s = float((fs.load_json("metadata/audio_analysis.json") or {}).get("duration_seconds") or 0.0)
        if audio_s <= 0:
            return None
        return estimate_remaining(profile, audio_s, progress.get("percent"))
    except Exception:  # noqa: BLE001 — l'ETA ne doit jamais casser le polling
        return None


_REPROCESSABLE_STATES = {
    JobState.COMPLETED.value,
    JobState.QUALITY_CHECKED.value,
    JobState.EXPORT_READY.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
}


@web_bp.route("/api/jobs/<job_id>/reprocess", methods=["POST"])
@login_required
def api_reprocess(job_id: str):
    """Relance le traitement d'un job déjà terminé (lexique modifié, prompt mis à jour…)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    if job.state not in _REPROCESSABLE_STATES:
        return jsonify({
            "error": "Le job ne peut pas être relancé dans son état actuel",
            "current_state": job.state,
        }), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Fichier audio introuvable"}), 400

    payload = request.get_json(silent=True) or {}
    processing_profile_id = payload.get("processing_profile_id")
    try:
        profile, mode = profiles.resolve_request(processing_profile_id, payload.get("mode", "fast"))
    except (KeyError, ValueError):
        return jsonify({"error": f"Profil/mode invalide: {processing_profile_id or payload.get('mode')}"}), 400

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    # Reprocess = run PROPRE (lexique/prompt modifiés) : vider l'état de reprise, sinon
    # le pipeline reprenable sauterait toutes les phases déjà faites → no-op silencieux.
    reset_resume_state(JobStore, job.id)

    vram_profile = PipelineService.estimate_profile_resources(cfg, profile)
    # Durée audio portée par l'entrée de file (DB) : la page File (non job-scoped) n'a pas
    # accès aux fichiers du job en mode frontale/nœud GPU — sans ça, l'estimation d'attente
    # serait vide en split. Cf. revue macro split.
    try:
        _aa = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).load_json("metadata/audio_analysis.json") or {}
        vram_profile["audio_seconds"] = float(_aa.get("duration_seconds") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort, l'attente retombe sur le fichier sinon
        pass
    try:
        result = executor.submit_process(
            job.id, str(audio_path), mode, vram_profile=vram_profile, processing_profile_id=profile.id
        )
    except TypeError as exc:
        # Compat (skew de version de l'exécuteur) : signature sans les kwargs récents.
        if "unexpected keyword argument" not in str(exc):
            raise
        result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409
    JobStore.update(job.id, processing_mode=mode)
    JobStore.update_state(job.id, JobState.READY_TO_PROCESS)

    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "processing_profile_id": profile.id,
        "reprocess": True,
    }), 202


@web_bp.route("/api/jobs/<job_id>/quality", methods=["POST"])
@login_required
def api_quality(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_quality_checks(job, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/export", methods=["POST"])
@login_required
def api_export(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.build_export(job, cfg)
    return jsonify(result)


@web_bp.route("/api/system/status")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def api_system_status():
    return jsonify(get_system_status())

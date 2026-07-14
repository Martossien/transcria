"""Observabilité : /health, /ready et /metrics (Prometheus).

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``. Les trois
partagent le même healthcheck base de données, d'où leur regroupement (les
modules de routes ne s'importent jamais entre eux).
"""
import logging
import time

from flask import Response, jsonify
from sqlalchemy import func

from transcria.database import db
from transcria.jobs import artifact_store
from transcria.jobs.models import Job
from transcria.queue.store import QueueStore
from transcria.services.job_executor import get_job_executor
from transcria.web.blueprint import web_bp

logger = logging.getLogger(__name__)

PROCESS_START_TIME = time.time()


def _check_database_health() -> tuple[bool, str | None]:
    try:
        db.session.execute(db.select(1)).scalar()
        return True, None
    except Exception as exc:
        logger.exception("Healthcheck base de données en échec")
        return False, str(exc)


def _collect_job_state_counts() -> dict[str, int]:
    rows = db.session.execute(
        db.select(Job.state, func.count(Job.id)).group_by(Job.state)
    ).all()
    return {state: count for state, count in rows}


def _render_prometheus_metrics() -> str:
    db_ok, _ = _check_database_health()
    state_counts = _collect_job_state_counts() if db_ok else {}
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else {
        "queued_jobs": 0,
        "running_jobs": 0,
        "max_workers": 0,
    }
    try:
        queue_counts = QueueStore.count_by_status() if db_ok else {}
    except Exception:
        queue_counts = {}
    try:
        blob_stats = artifact_store.store_stats() if db_ok else {"files": 0, "bytes": 0}
    except Exception:
        blob_stats = {"files": 0, "bytes": 0}
    lines = [
        "# HELP transcria_up Indique si le service TranscrIA est disponible.",
        "# TYPE transcria_up gauge",
        f"transcria_up {1 if db_ok else 0}",
        "# HELP transcria_ready Indique si le service accepte de nouveaux jobs.",
        "# TYPE transcria_ready gauge",
        f"transcria_ready {1 if db_ok and executor is not None else 0}",
        "# HELP transcria_process_start_time_seconds Horodatage Unix du démarrage du process web.",
        "# TYPE transcria_process_start_time_seconds gauge",
        f"transcria_process_start_time_seconds {PROCESS_START_TIME:.0f}",
        "# HELP transcria_jobs_total Nombre total de jobs en base.",
        "# TYPE transcria_jobs_total gauge",
        f"transcria_jobs_total {sum(state_counts.values())}",
        "# HELP transcria_worker_jobs Nombre de jobs suivis par le worker interne.",
        "# TYPE transcria_worker_jobs gauge",
        f'transcria_worker_jobs{{status="queued"}} {runtime["queued_jobs"]}',
        f'transcria_worker_jobs{{status="running"}} {runtime["running_jobs"]}',
        "# HELP transcria_worker_capacity Nombre maximal de jobs simultanés pour le worker interne.",
        "# TYPE transcria_worker_capacity gauge",
        f"transcria_worker_capacity {runtime['max_workers']}",
        "# HELP transcria_queue_entries Nombre d'entrées dans la file persistante.",
        "# TYPE transcria_queue_entries gauge",
        f'transcria_queue_entries{{status="waiting"}} {queue_counts.get("waiting", 0)}',
        f'transcria_queue_entries{{status="paused"}} {queue_counts.get("paused", 0)}',
        f'transcria_queue_entries{{status="running"}} {queue_counts.get("running", 0)}',
        "# HELP transcria_job_files_total Fichiers de jobs répliqués en base (storage.shared_backend=pg ; 0 en fs).",
        "# TYPE transcria_job_files_total gauge",
        f"transcria_job_files_total {blob_stats['files']}",
        "# HELP transcria_job_files_bytes Volume (octets) des fichiers de jobs répliqués en base "
        "(croissance continue = purge input/ qui ne joue pas).",
        "# TYPE transcria_job_files_bytes gauge",
        f"transcria_job_files_bytes {blob_stats['bytes']}",
        "# HELP transcria_jobs_state Nombre de jobs par état.",
        "# TYPE transcria_jobs_state gauge",
    ]
    for state in sorted(state_counts):
        lines.append(f'transcria_jobs_state{{state="{state}"}} {state_counts[state]}')
    return "\n".join(lines) + "\n"


@web_bp.route("/health")
def health():
    db_ok, db_error = _check_database_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "transcria",
        "database": {
            "status": "ok" if db_ok else "error",
        },
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if db_ok else 503)


@web_bp.route("/ready")
def ready():
    db_ok, db_error = _check_database_health()
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else None
    ready_ok = db_ok and executor is not None
    payload = {
        "status": "ready" if ready_ok else "not_ready",
        "service": "transcria",
        "database": {"status": "ok" if db_ok else "error"},
        "worker": runtime or {"healthy": False},
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if ready_ok else 503)


@web_bp.route("/metrics")
def metrics():
    return Response(_render_prometheus_metrics(), mimetype="text/plain; version=0.0.4; charset=utf-8")

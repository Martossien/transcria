"""Alertes destinées aux administrateurs (indépendantes du propriétaire du job).

Aujourd'hui : alerte « job en attente de VRAM » — la mémoire GPU locale est
momentanément insuffisante (condition transitoire). Le job n'échoue pas, il patiente
et reprend automatiquement ; l'admin est prévenu pour libérer de la VRAM s'il le
souhaite. Voir docs/SERVICE_RESSOURCES_GPU.md.
"""

from __future__ import annotations

from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.logging_setup import get_structured_logger
from transcria.notifications.mailer import send_admin_vram_alert_async


def get_admin_emails() -> list[str]:
    """Emails des administrateurs globaux actifs (best-effort, dédoublonnés)."""
    try:
        emails = [
            (u.email or "").strip()
            for u in UserStore.list_users(active_only=True)
            if u.role == Role.ADMIN.value
        ]
        return [e for e in dict.fromkeys(emails) if e]
    except Exception:  # noqa: BLE001 — hors app context / DB indispo : non bloquant
        return []


def alert_admin_vram_wait(cfg: dict, job, *, required_mb: int, phase: str) -> None:
    """Trace un WARNING dédié et alerte les admins par email qu'un job attend la VRAM.

    Best-effort : ne lève jamais. À n'appeler qu'à la PREMIÈRE entrée en attente d'un
    job (l'anti-spam est géré par `mark_execution_waiting_vram`).
    """
    job_id = getattr(job, "id", "") or ""
    job_title = getattr(job, "title", "") or ""
    sl = get_structured_logger(__name__)
    sl.warning(
        "Job en attente de VRAM — administrateur alerté",
        job_id=job_id,
        required_vram_mb=int(required_mb),
        phase=phase,
    )
    try:
        send_admin_vram_alert_async(
            cfg,
            admin_emails=get_admin_emails(),
            job_title=job_title,
            job_id=job_id,
            required_mb=int(required_mb),
            phase=phase,
        )
    except Exception as exc:  # noqa: BLE001
        sl.warning("Alerte VRAM admin non envoyée (job=%s): %s", job_id, exc)

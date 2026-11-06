"""Purge de rétention — l'UNIQUE endroit qui sait tout ce qui expire (RGPD/DPO, docs/AUDIT_DPO.md).

Appelée au chargement de la page d'accueil et par ``maintenance.cli purge`` : jobs terminés
(``security.retention_days``), entrées d'audit (``security.audit_retention_days`` par famille),
mémoire courte de « Discuter avec mes réunions » (``workflow.ask_meetings.retention_days``).
Une donnée nouvelle qui expire s'ajoute ICI, pas dans un appelant.
"""
from __future__ import annotations

from dataclasses import dataclass

from transcria.ask_meetings.store import AskStore
from transcria.audit.store import AuditStore
from transcria.config.views import AskMeetingsView
from transcria.jobs.store import JobStore


@dataclass(frozen=True)
class PurgeReport:
    jobs: int
    audit_entries: int
    ask_items: int


def purge_expired(cfg: dict, *, dry_run: bool = False) -> PurgeReport:
    """Applique les trois rétentions. ``dry_run`` ne fait que compter les jobs (les autres purges
    n'ont pas de simulation : elles rendent 0)."""
    sec = cfg.get("security", {}) or {}
    jobs = JobStore.purge_expired_jobs(sec.get("retention_days"), cfg["storage"]["jobs_dir"], dry_run=dry_run)
    if dry_run:
        return PurgeReport(jobs=jobs, audit_entries=0, ask_items=0)
    audit_entries = 0
    audit_days = sec.get("audit_retention_days", 1095)
    if isinstance(audit_days, (int, float)) and audit_days > 0:
        audit_entries = AuditStore.purge_expired_by_policy(int(audit_days), sec.get("audit_retention_by_family") or {})
    ask_items = AskStore.purge_expired(AskMeetingsView.from_config(cfg).retention_days)
    return PurgeReport(jobs=jobs, audit_entries=audit_entries, ask_items=ask_items)

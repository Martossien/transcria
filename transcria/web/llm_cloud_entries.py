"""Garde LLM cloud des ENTRÉES web (docs/LLM_CLOUD.md § 3.2.4-5) — refus à la soumission.

Helper PARTAGÉ par les modules de routes qui peuvent déclencher une phase LLM (traitement,
résumé et son autostart, affinage, synchronisation de synthèse de l'éditeur, ingestion par
connecteur) et par les vues de profils (grisage). Un seul juge : ``check_cloud_gate`` ; ce
module ne fait que calculer ``needs_llm`` et rendre la réponse 403 JSON homogène
``{"error": <message FR>, "code": <code>}``. Jamais de repli sur une LLM locale.
"""
from __future__ import annotations

import logging

from flask import jsonify

from transcria.config.views import WorkflowView
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.llm_cloud.gate import check_cloud_gate
from transcria.llm_cloud.policy import CloudGate
from transcria.llm_cloud.store import LlmCloudStore

logger = logging.getLogger(__name__)


def profile_needs_llm(cfg: dict, profile) -> bool:
    """Le traitement complet de ce profil sollicitera-t-il la LLM d'arbitrage ?

    Profil sans exigences déclarées = on suppose que oui (prudence : seul le mode cloud
    en dépend, la garde est neutre hors cloud)."""
    requirements = getattr(profile, "resource_requirements", None)
    needs = bool(getattr(requirements, "needs_llm", True))
    return needs and WorkflowView.from_config(cfg).arbitration_llm_enabled


def summary_needs_llm(cfg: dict) -> bool:
    """Le résumé de contrôle sollicitera-t-il la LLM (``workflow.summary_llm.enabled``) ?"""
    return WorkflowView.from_config(cfg).summary_llm_enabled


def cloud_gate(cfg: dict, *, owner_id: str | None, job_extra_data: dict | None, needs_llm: bool) -> CloudGate:
    """Décision du juge unique pour le PROPRIÉTAIRE du job (pas le demandeur)."""
    return check_cloud_gate(cfg, owner_id=owner_id, job_extra_data=job_extra_data, needs_llm=needs_llm)


def refusal_response(gate: CloudGate, *, entry: str, job_id: str | None = None):
    """Réponse 403 JSON d'un refus (+ WARNING : certaines entrées n'ont personne pour lire)."""
    logger.warning("[llm-cloud] %s refusé à la soumission (job=%s, code=%s) : %s",
                   entry, job_id or "-", gate.code, gate.message_fr)
    return jsonify({"error": gate.message_fr, "code": gate.code}), 403


def job_cloud_refusal(cfg: dict, job, *, needs_llm: bool, entry: str):
    """Refus 403 pour un job EXISTANT, ou None si la soumission est admissible."""
    gate = cloud_gate(cfg, owner_id=job.owner_id, job_extra_data=job.get_extra_data(), needs_llm=needs_llm)
    if gate.allowed:
        return None
    return refusal_response(gate, entry=entry, job_id=job.id)


def owner_cloud_forbidden(cfg: dict, owner_id: str | None) -> bool:
    """Le propriétaire est-il interdit de LLM cloud ? Toujours False hors mode cloud (aucune
    requête en base) — sert au grisage des profils à LLM."""
    if not is_cloud_backend(cfg):
        return False
    return LlmCloudStore.user_forbidden(owner_id)

"""Trace par job de l'usage d'une LLM externe (docs/LLM_CLOUD.md § 3.2.6).

Fichier ``metadata/llm_provenance.json`` : ``{provider, model, hosting, external: true,
phases: [{name, at}]}`` — écrit par l'exécuteur HORS de la fenêtre de surveillance d'un
``AgentWorkspace`` (une écriture sous ``metadata/`` pendant un run d'agent serait imputée à
l'agent). Miroir ``extra_data.llm_provider`` pour l'interface, action d'audit
``job_external_llm`` (acteur système, propriétaire dans les détails). Nouveau fichier runtime →
``docs/DATA_MODEL.md``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from transcria.audit.models import AuditAction
from transcria.audit.store import AuditStore
from transcria.config.llm_providers import get_llm_provider
from transcria.config.views import LlmCloudView
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore

logger = logging.getLogger(__name__)

PROVENANCE_PATH = "metadata/llm_provenance.json"

# Phases du pipeline complet qui sollicitent la LLM d'arbitrage (noms de `completed_phases`).
# `final_review` englobe l'extraction des champs de type (appel direct dans la même phase).
LLM_PIPELINE_PHASES = ("correction", "final_review")


def phases_for_mode(
    mode: str,
    completed_phases: list[str] | tuple[str, ...] | None,
    *,
    summary_llm_enabled: bool = True,
    multi_stt_enabled: bool = False,
    llm_attempted: bool = True,
) -> list[str]:
    """Phases LLM réellement concernées par une exécution.

    - `summary` : la LLM de résumé si elle est activée ET sollicitée (`llm_attempted` : succès ou
      échec APRÈS envoi — une panne STT avant la LLM n'envoie rien) ;
    - `refine` : toujours (le tour part chez le fournisseur) ;
    - pipeline complet : phases LLM ACHEVÉES (`completed_phases`), plus l'arbitrage multi-STT
      (appels directs AVANT la correction, non marqué comme phase de reprise) quand il est activé
      et que la correction a eu lieu.
    """
    if mode == "summary":
        return ["summary_llm"] if (summary_llm_enabled and llm_attempted) else []
    if mode == "refine":
        return ["refine"]
    if mode == "speakers":
        return []
    done = set(completed_phases or ())
    phases = [p for p in LLM_PIPELINE_PHASES if p in done]
    if multi_stt_enabled and "correction" in done:
        phases.insert(0, "multi_stt_review")
    return phases


def record_external_llm_use(config: dict, job_id: str, phases: list[str]) -> dict | None:
    """Ajoute les phases à la provenance du job et pose le miroir + l'audit. Best-effort :
    ne lève jamais (la trace ne doit pas faire échouer un traitement réussi). ``None`` hors
    cloud ou sans phase."""
    if not phases or not is_cloud_backend(config):
        return None
    try:
        view = LlmCloudView.from_config(config)
        listed = get_llm_provider(view.provider)
        fs = JobFilesystem(config["storage"]["jobs_dir"], job_id)
        data = fs.load_json(PROVENANCE_PATH) or {}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        entries = list(data.get("phases") or [])
        # Fournisseur et modèle PAR phase : une bascule A → B entre deux phases ne doit jamais
        # réécrire l'histoire (la correction partie chez A reste attribuée à A). La racine
        # décrit le DERNIER usage ; `providers` liste tous les fournisseurs vus.
        entries.extend({"name": name, "at": now, "provider": view.provider, "model": view.model}
                       for name in phases)
        providers = sorted({str(e.get("provider") or "") for e in entries if e.get("provider")})
        data = {
            "provider": view.provider,
            "provider_label": listed.label if listed else view.provider,
            "model": view.model,
            "hosting": listed.hosting_text("fr") if listed else "",
            "hosting_en": listed.hosting_text("en") if listed else "",
            "external": True,
            "providers": providers,
            "phases": entries,
        }
        fs.save_json(PROVENANCE_PATH, data)
        job = JobStore.update_extra_data(
            job_id,
            lambda extra: {**extra, "llm_provider": {"provider": view.provider, "model": view.model,
                                                      "external": True}},
        )
        AuditStore.log(
            AuditAction.JOB_EXTERNAL_LLM, target_type="job", target_id=job_id,
            target_label=getattr(job, "title", "") or "",
            details={"provider": view.provider, "model": view.model, "phases": phases,
                     "owner_id": getattr(job, "owner_id", None)},
        )
        return data
    except Exception:  # noqa: BLE001 — trace best-effort, jamais une cause d'échec
        logger.warning("Provenance LLM cloud non écrite pour le job %s", job_id, exc_info=True)
        return None

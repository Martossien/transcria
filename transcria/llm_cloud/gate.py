"""Garde d'admission LLM cloud — assemble la config (vue typée), la base (store) et la politique.

Deux usages, UN seul juge :
- côté web, chaque entrée qui peut déclencher une phase LLM (traitement complet, autostart et
  résumé manuel, affinage, synchronisation de synthèse, ingestion par connecteur) appelle
  :func:`check_cloud_gate` et refuse avec le message FR qui guide ;
- côté worker, ``job_executor`` rappelle la MÊME garde avant toute phase LLM (dernier rempart :
  un job enfilé avant l'interdiction est refusé avec un état explicite).
Hors cloud, la garde répond « autorisé » sans toucher à la base.
"""
from __future__ import annotations

from transcria.config.llm_providers import get_llm_provider
from transcria.config.views import LlmCloudView
from transcria.database import db
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.llm_cloud.policy import (
    CLOUD_NOTICE_TEXT_VERSION,
    CloudGate,
    ack_is_valid,
    decide_gate,
    job_from_connector,
)
from transcria.llm_cloud.store import LlmCloudStore


def _ack_valid_for(ack, view: LlmCloudView) -> bool:
    """Validité calculée sur la ligne DÉJÀ chargée (une seule requête par rendu)."""
    if ack is None:
        return False
    return ack_is_valid(
        ack_provider=ack.provider, ack_model=ack.model, ack_text_version=ack.text_version,
        ack_revoked=ack.revoked_at is not None, provider=view.provider, model=view.model,
    )


def cloud_status(config: dict, lang: str = "fr") -> dict:
    """État pour l'interface (bandeau, écran admin) : actif ?, fournisseur, modèle, hébergement,
    validé ?, connecteurs autorisés ?. Lecture seule, une requête au plus, jamais de réseau."""
    active = is_cloud_backend(config)
    if not active:
        return {"active": False}
    view = LlmCloudView.from_config(config)
    listed = get_llm_provider(view.provider)
    try:
        ack = LlmCloudStore.current(view.provider, view.model)
    except Exception:  # noqa: BLE001 — base indisponible : bandeau DÉGRADÉ, jamais absent en cloud
        # Transaction PostgreSQL avortée : sans rollback, les requêtes suivantes de la même
        # requête HTTP échoueraient toutes.
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"active": True, "unknown": True, "acknowledged": False,
                "provider": view.provider, "provider_label": listed.label if listed else view.provider,
                "model": view.model, "hosting": listed.hosting_text(lang) if listed else "",
                "facts": [], "terms_url": "", "connectors_allowed": False,
                "text_version": CLOUD_NOTICE_TEXT_VERSION}
    acknowledged = _ack_valid_for(ack, view)
    return {
        "active": True,
        "provider": view.provider,
        "provider_label": listed.label if listed else view.provider,
        "model": view.model,
        "base_url": view.base_url,
        "known_provider": listed is not None,
        "hosting": listed.hosting_text(lang) if listed else "",
        "facts": list(listed.facts_text(lang)) if listed else [],
        "terms_url": listed.terms_url if listed else "",
        "acknowledged": acknowledged,
        "stale_acknowledgement": ack is not None and not acknowledged,
        "acknowledgement": ack.to_dict() if ack is not None else None,
        "connectors_allowed": bool(ack.connectors_allowed) if (ack is not None and acknowledged) else False,
        "text_version": CLOUD_NOTICE_TEXT_VERSION,
    }


def check_cloud_gate(
    config: dict,
    *,
    owner_id: str | None,
    job_extra_data: dict | None = None,
    needs_llm: bool = True,
) -> CloudGate:
    """Décision d'admission pour un traitement du job de ``owner_id`` (propriétaire, pas le
    demandeur : un admin qui relance le job d'un membre interdit ne lève pas l'interdiction)."""
    if not is_cloud_backend(config):
        return decide_gate(cloud_active=False, needs_llm=needs_llm, provider_resolved=True,
                           acknowledged=True, forbidden=False, from_connector=False,
                           connectors_allowed=True)
    if not needs_llm:
        return decide_gate(cloud_active=True, needs_llm=False, provider_resolved=True,
                           acknowledged=True, forbidden=False, from_connector=False,
                           connectors_allowed=True)
    view = LlmCloudView.from_config(config)
    resolved = bool(view.provider and view.model)
    ack = LlmCloudStore.current(view.provider, view.model) if resolved else None
    acknowledged = resolved and _ack_valid_for(ack, view)
    return decide_gate(
        cloud_active=True,
        needs_llm=True,
        provider_resolved=resolved,
        acknowledged=acknowledged,
        forbidden=LlmCloudStore.user_forbidden(owner_id),
        from_connector=job_from_connector(job_extra_data),
        connectors_allowed=bool(ack.connectors_allowed) if ack is not None else False,
    )

"""Disponibilité des profils de traitement pour l'UI (Phase 6).

Source UNIQUE (backend) de « quels profils peut-on lancer ici, et lequel recommander ».
L'UI ne duplique aucune règle : elle consomme `compute_profiles_view(config)`.

Le statut est STRUCTUREL (dérivé de la config + topologie), pas transitoire : il dit si un
profil peut s'exécuter sur cette installation, pas s'il y a de la VRAM libre à la seconde près
(l'attente VRAM reste gérée par le scheduler / `waiting_vram`). Statuts émis :

- ``available``          : lançable localement ;
- ``available_remote``   : lançable via le nœud de ressources (topologie split) ;
- ``unavailable``        : impossible sans changer l'installation (ex. LLM non configurée) ;
- ``disabled_by_config`` : désactivé par la configuration (ex. mode qualité coupé).

Le profil RECOMMANDÉ est le **plus élevé qui passe** (le « maximum » validé par la config /
le matériel), en respectant un éventuel défaut configuré s'il est disponible.
"""
from __future__ import annotations

from transcria.workflow.profiles import (
    DEFAULT_PROFILE_ID,
    ProcessingProfile,
    list_profiles,
    profile_deliverables,
    profile_validations,
)

_AVAILABLE_STATUSES = ("available", "available_remote")


def _llm_reachable(config: dict) -> bool:
    """La LLM d'arbitrage (résumé/correction) est-elle activée ?"""
    return config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is not False


def _quality_mode_enabled(config: dict) -> bool:
    return config.get("workflow", {}).get("enable_quality_mode", True) is not False


def _remote_configured(config: dict) -> bool:
    """Une topologie distante (nœud de ressources) est-elle configurée ?"""
    try:
        from transcria.inference.resource_status import remote_requirements

        return bool(remote_requirements(config))
    except Exception:  # noqa: BLE001 — best-effort : à défaut, on suppose le local
        return False


def _enabled_ids(config: dict) -> set[str] | None:
    """Liste blanche de profils activés en config (`workflow.profiles.enabled`), ou None."""
    enabled = (config.get("workflow", {}).get("profiles", {}) or {}).get("enabled")
    if isinstance(enabled, list) and enabled:
        return {str(x) for x in enabled}
    return None


def profile_status(profile: ProcessingProfile, config: dict) -> tuple[str, list[str]]:
    """Statut structurel d'un profil + raisons (FR) le cas échéant."""
    rr = profile.resource_requirements
    if rr.needs_llm and not _llm_reachable(config):
        return "unavailable", ["LLM d'arbitrage non configurée"]
    if rr.needs_diarization and not _quality_mode_enabled(config):
        return "disabled_by_config", ["Mode qualité désactivé dans la configuration"]
    if _remote_configured(config):
        return "available_remote", []
    return "available", []


def compute_profiles_view(config: dict) -> dict:
    """Vue complète des profils pour l'UI : statut, livrables, et profil recommandé.

    `recommended` = profil disponible de plus haut niveau (le maximum qui passe), en privilégiant
    le défaut configuré s'il est disponible. None si aucun profil n'est lançable.
    """
    enabled = _enabled_ids(config)
    configured_default = (config.get("workflow", {}).get("profiles", {}) or {}).get("default") or DEFAULT_PROFILE_ID

    items: list[dict] = []
    available_by_level: list[ProcessingProfile] = []
    for profile in list_profiles():  # triés par niveau, sans legacy
        if enabled is not None and profile.id not in enabled:
            status, reasons = "disabled_by_config", ["Profil désactivé dans la configuration"]
        else:
            status, reasons = profile_status(profile, config)
        is_available = status in _AVAILABLE_STATUSES
        if is_available:
            available_by_level.append(profile)
        items.append({
            "id": profile.id,
            "label": profile.label,
            "description": profile.description,
            "level": profile.level,
            "status": status,
            "available": is_available,
            "reasons": reasons,
            "deliverables": profile_deliverables(profile),
            "validations": profile_validations(profile),
        })

    # Recommandé = le profil disponible de PLUS HAUT niveau (« le maximum que le matériel /
    # la config valide »). L'utilisateur peut redescendre le curseur pour aller plus vite.
    recommended: str | None = (
        max(available_by_level, key=lambda p: p.level).id if available_by_level else None
    )

    return {"profiles": items, "recommended": recommended, "default": configured_default}

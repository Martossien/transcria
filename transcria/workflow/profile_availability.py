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
from transcria.workflow.profiles_i18n import localize_profile_text

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


def compute_profiles_view(config: dict, language: str | None = None) -> dict:
    """Vue complète des profils pour l'UI : statut, livrables, et profil recommandé.

    `recommended` = profil disponible de plus haut niveau (le maximum qui passe), en privilégiant
    le défaut configuré s'il est disponible. None si aucun profil n'est lançable.

    `language` = locale d'AFFICHAGE (UI). Toutes les chaînes user-facing (label/description/
    livrables/validations/raisons) sont localisées via `localize_profile_text` ; l'`id` reste la
    clé logique. Défaut `None`/`fr` = sortie FR historique inchangée.
    """
    enabled = _enabled_ids(config)
    configured_default = (config.get("workflow", {}).get("profiles", {}) or {}).get("default") or DEFAULT_PROFILE_ID

    def _tr(text: str) -> str:
        return localize_profile_text(text, language)

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
            "label": _tr(profile.label),
            "description": _tr(profile.description),
            "level": profile.level,
            "status": status,
            "available": is_available,
            "reasons": [_tr(r) for r in reasons],
            "deliverables": [_tr(d) for d in profile_deliverables(profile)],
            "validations": [_tr(v) for v in profile_validations(profile)],
        })

    # Recommandé = le profil disponible de PLUS HAUT niveau (« le maximum que le matériel /
    # la config valide »). L'utilisateur peut redescendre le curseur pour aller plus vite.
    recommended: str | None = (
        max(available_by_level, key=lambda p: p.level).id if available_by_level else None
    )

    return {"profiles": items, "recommended": recommended, "default": configured_default}


# Étapes de PRÉPARATION humaine, dans l'ordre linéaire du wizard. Un profil n'en exige qu'un
# PRÉFIXE (cf. `profile_required_steps_ordered`) ; le reste est un SUFFIXE optionnel, qu'on
# regroupe sous un repli « Étapes optionnelles pour ce profil ». La propriété préfixe/suffixe
# garantit que la chaîne de révélation progressive du wizard reste continue.
PREP_STEPS: tuple[str, ...] = ("summary", "context", "participants", "lexicon")
_CORE_HEAD: tuple[str, ...] = ("file", "analyze")
_CORE_TAIL: tuple[str, ...] = ("processing", "quality", "export")


def compute_wizard_layout(profile: ProcessingProfile | None, statuses: dict) -> dict:
    """Disposition du wizard PILOTÉE PAR LE PROFIL (choisi à l'étape 1).

    Le moteur d'états (`WORKFLOW_STEPS`, `compute_statuses`) reste inchangé : on ne touche QUE
    la présentation. À partir des exigences du profil :

    - ``required_prep`` : préfixe d'étapes de préparation à afficher dans le flux principal ;
    - ``optional_prep`` : suffixe à masquer sous un repli (l'utilisateur peut quand même les
      renseigner) ;
    - ``first_optional`` : 1re étape du repli (pour ouvrir le ``<details>`` au bon endroit) ;
    - ``step_num`` : numéro d'affichage 1..N des seules étapes VISIBLES (renumérotation) ;
    - ``display_steps`` : étapes de la barre de progression (visibles, renumérotées) ;
    - ``launch_ready`` : le profil peut-il être lancé maintenant (analyse + préfixe validés).

    ``profile`` à None (job legacy/sans profil) ⇒ comportement complet : toutes les étapes de
    préparation sont requises (rétro-compatibilité, aucun parcours existant n'est raccourci).
    """
    from transcria.workflow.profiles import profile_required_steps_ordered
    from transcria.workflow.states import StepStatus
    from transcria.workflow.steps import WorkflowSteps

    if profile is None:
        required_prep = list(PREP_STEPS)
    else:
        ordered = set(profile_required_steps_ordered(profile))
        required_prep = [s for s in PREP_STEPS if s in ordered]
    optional_prep = [s for s in PREP_STEPS if s not in required_prep]

    visible = list(_CORE_HEAD) + required_prep + list(_CORE_TAIL)
    step_num = {sid: i + 1 for i, sid in enumerate(visible)}

    labels = {sid: (WorkflowSteps.get_step(sid) or {}).get("label", sid) for sid in visible}
    display_steps = [{"id": sid, "label": labels[sid], "order": step_num[sid]} for sid in visible]

    def _done(step_id: str) -> bool:
        return statuses.get(step_id) == StepStatus.DONE

    launch_ready = _done("analyze") and all(_done(s) for s in required_prep)

    return {
        "required_prep": required_prep,
        "optional_prep": optional_prep,
        "first_optional": optional_prep[0] if optional_prep else None,
        "step_num": step_num,
        "display_steps": display_steps,
        "launch_ready": launch_ready,
    }

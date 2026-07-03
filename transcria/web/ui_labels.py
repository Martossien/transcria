"""Libellés français des états de job pour l'interface (docs/archive/REFONTE_UI.md).

Source unique : aucun template ne doit afficher un état brut (`ready_to_process`…).
Exposé aux templates via les filtres Jinja `state_label` et `state_badge`
(enregistrés dans `transcria/web/routes.py`).
"""

from __future__ import annotations

# État JobState → libellé utilisateur. Les états « en cours » portent une ellipse.
JOB_STATE_LABELS: dict[str, str] = {
    "created": "Créé",
    "uploaded": "Fichier reçu",
    "analyzed": "Audio analysé",
    "summary_running": "Résumé en cours…",
    "summary_done": "Résumé prêt",
    "context_done": "Contexte renseigné",
    "participants_done": "Participants renseignés",
    "lexicon_done": "Lexique prêt",
    "speaker_detection_running": "Détection des locuteurs…",
    "speaker_detection_done": "Locuteurs détectés",
    "ready_to_process": "Prêt à traiter",
    "transcribing": "Transcription…",
    "diarizing": "Identification des locuteurs…",
    "arbitrating": "Correction LLM…",
    "quality_checking": "Contrôle qualité…",
    "quality_checked": "Qualité vérifiée",
    "export_ready": "Export prêt",
    "completed": "Terminé",
    "failed": "Échec",
    "cancelled": "Annulé",
}

_RUNNING_STATES = {
    "summary_running", "speaker_detection_running", "transcribing",
    "diarizing", "arbitrating", "quality_checking",
}

# État → classe Bootstrap du badge (text-bg-*).
_BADGE_CLASSES: dict[str, str] = {
    "completed": "success",
    "export_ready": "success",
    "failed": "danger",
    "cancelled": "secondary",
    "ready_to_process": "primary",
}


def state_label(state: str | None) -> str:
    """Libellé français d'un état de job (l'état brut en dernier recours, jamais vide)."""
    return JOB_STATE_LABELS.get(str(state or ""), str(state or "inconnu"))


def state_badge(state: str | None) -> str:
    """Classe de couleur Bootstrap (`text-bg-…`) cohérente pour le badge d'état."""
    key = str(state or "")
    if key in _BADGE_CLASSES:
        return f"text-bg-{_BADGE_CLASSES[key]}"
    if key in _RUNNING_STATES:
        return "text-bg-info"
    return "text-bg-secondary"

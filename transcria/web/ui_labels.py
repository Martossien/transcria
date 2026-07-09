"""Libellés français des états de job pour l'interface (docs/archive/REFONTE_UI.md).

Source unique : aucun template ne doit afficher un état brut (`ready_to_process`…).
Exposé aux templates via les filtres Jinja `state_label` et `state_badge`
(enregistrés dans `transcria/web/routes.py`).
"""

from __future__ import annotations

from flask_babel import gettext

from transcria.web.i18n_js import N_

# État JobState → libellé utilisateur. Les états « en cours » portent une ellipse. Les valeurs
# sont marquées `N_` (extraites par babel, source FR inchangée) et TRADUITES au rendu dans
# `state_label` via `gettext` — sinon la liste des jobs affichait « Terminé » même en UI EN.
JOB_STATE_LABELS: dict[str, str] = {
    "created": N_("Créé"),
    "uploaded": N_("Fichier reçu"),
    "analyzed": N_("Audio analysé"),
    "summary_running": N_("Résumé en cours…"),
    "summary_done": N_("Résumé prêt"),
    "context_done": N_("Contexte renseigné"),
    "participants_done": N_("Participants renseignés"),
    "lexicon_done": N_("Lexique prêt"),
    "speaker_detection_running": N_("Détection des locuteurs…"),
    "speaker_detection_done": N_("Locuteurs détectés"),
    "ready_to_process": N_("Prêt à traiter"),
    "transcribing": N_("Transcription…"),
    "diarizing": N_("Identification des locuteurs…"),
    "arbitrating": N_("Correction LLM…"),
    "quality_checking": N_("Contrôle qualité…"),
    "quality_checked": N_("Qualité vérifiée"),
    "export_ready": N_("Export prêt"),
    "completed": N_("Terminé"),
    "failed": N_("Échec"),
    "cancelled": N_("Annulé"),
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
    """Libellé LOCALISÉ d'un état de job (l'état brut en dernier recours, jamais vide).

    Traduit à l'appel (filtre Jinja = contexte requête) ; hors contexte, `gettext` renvoie la
    source FR — comportement historique préservé."""
    label = JOB_STATE_LABELS.get(str(state or ""))
    if label is None:
        return str(state or "inconnu")
    return gettext(label)


def state_badge(state: str | None) -> str:
    """Classe de couleur Bootstrap (`text-bg-…`) cohérente pour le badge d'état."""
    key = str(state or "")
    if key in _BADGE_CLASSES:
        return f"text-bg-{_BADGE_CLASSES[key]}"
    if key in _RUNNING_STATES:
        return "text-bg-info"
    return "text-bg-secondary"

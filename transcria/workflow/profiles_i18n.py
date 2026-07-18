"""Traductions d'affichage des profils de traitement (axe A — UI).

Les libellés, descriptions, livrables et validations des profils vivent en FR dans
``profiles.py`` (la clé logique reste ``profile.id``). Cette table fournit l'AFFICHAGE
localisé sans jamais toucher aux données du registre — même principe que
``localized_builtin_types`` pour les types de réunion : on traduit à l'affichage, la clé
logique ne bouge pas.

``fr`` = identité stricte (le défaut historique est inchangé octet pour octet). Une chaîne
absente de la table est renvoyée telle quelle (repli sûr), donc ajouter une nouvelle valeur
FR dans ``profiles.py`` ne casse jamais l'UI : elle s'affichera en FR tant qu'on ne l'a pas
traduite ici.
"""
from __future__ import annotations

# Traductions anglaises de toutes les chaînes d'affichage produites par le registre des
# profils (`profiles.py`) et par `profile_availability.profile_status`. Terminologie alignée
# sur le catalogue Babel EN (Glossary/Speakers/Meeting context/Review summary…).
_EN: dict[str, str] = {
    # Libellés de profil (profile.label).
    "SRT express": "Express SRT",
    "SRT avec locuteurs": "SRT with speakers",
    "SRT locuteurs une passe (MOSS)": "Single-pass speaker SRT (MOSS)",
    "Word rapide": "Quick Word",
    "Word structuré": "Structured Word",
    "Word corrigé": "Corrected Word",
    "Dossier qualité complet": "Full quality package",
    # Descriptions de profil (profile.description).
    "Transcription brute, le plus vite possible. Aucune validation.":
        "Raw transcription, as fast as possible. No validation.",
    "Transcription attribuée aux locuteurs. Validation des locuteurs.":
        "Transcription attributed to speakers. Speaker validation.",
    "Transcription ET locuteurs en une seule passe GPU (MOSS), réservée aux "
    "réunions courtes (10 min par défaut). Aucune validation wizard : la voie "
    "la plus directe pour un SRT attribué. Omissions et troncatures du modèle "
    "surveillées (alertes qualité).":
        "Transcription AND speakers in a single GPU pass (MOSS), reserved for "
        "short meetings (10 min by default). No wizard validation: the most "
        "direct route to an attributed SRT. Model omissions and truncations are "
        "monitored (quality alerts).",
    "Compte rendu Word présentable rapidement, validation minimale.":
        "Presentable Word minutes, quickly, with minimal validation.",
    "Word template avec participants et structure de réunion, sans correction SRT.":
        "Word template with participants and meeting structure, without SRT correction.",
    "Word + SRT corrigés (correction LLM), lexique optionnel.":
        "Corrected Word + SRT (LLM correction), optional glossary.",
    "Workflow complet : qualité maximale, lexique validé, ZIP complet.":
        "Full workflow: maximum quality, validated glossary, complete ZIP.",
    # Livrables (profile_deliverables).
    "SRT": "SRT",
    "SRT corrigé": "Corrected SRT",
    "Segments JSON": "JSON segments",
    "Word (template de base)": "Word (basic template)",
    "Word enrichi": "Enriched Word",
    "Word complet": "Full Word",
    "Rapport qualité complet": "Full quality report",
    "Archive ZIP complète": "Complete ZIP archive",
    # Validations humaines (profile_validations).
    "Résumé de contrôle": "Review summary",
    "Contexte de réunion": "Meeting context",
    "Participants": "Participants",
    "Validation des locuteurs": "Speaker validation",
    "Lexique de session": "Session glossary",
    "Lexique (optionnel)": "Glossary (optional)",
    # Raisons d'indisponibilité (profile_status).
    "LLM d'arbitrage non configurée": "Arbitration LLM not configured",
    "Backend STT 'moss' non activé dans la configuration":
        "STT backend 'moss' not enabled in the configuration",
    "Mode qualité désactivé dans la configuration": "Quality mode disabled in the configuration",
    "Profil désactivé dans la configuration": "Profile disabled in the configuration",
}


def localize_profile_text(text: str, language: str | None) -> str:
    """Traduit une chaîne d'affichage de profil vers la locale UI (repli = FR inchangé)."""
    if language == "en":
        return _EN.get(text, text)
    return text

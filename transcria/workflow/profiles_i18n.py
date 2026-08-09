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


_DE: dict[str, str] = {
    "SRT express": "Express-SRT",
    "SRT avec locuteurs": "SRT mit Sprechern",
    "SRT locuteurs une passe (MOSS)": "SRT mit Sprechern in einem Durchgang (MOSS)",
    "Word rapide": "Schnelles Word",
    "Word structuré": "Strukturiertes Word",
    "Word corrigé": "Korrigiertes Word",
    "Dossier qualité complet": "Vollständiges Qualitätspaket",
    "Transcription brute, le plus vite possible. Aucune validation.": "Rohtranskription, so schnell wie möglich. Keine Validierung.",
    "Transcription attribuée aux locuteurs. Validation des locuteurs.": "Den Sprechern zugeordnete Transkription. Sprechervalidierung.",
    "Transcription ET locuteurs en une seule passe GPU (MOSS), réservée aux réunions courtes (10 min par défaut). "
        "Aucune validation wizard : la voie la plus directe pour un SRT attribué. Omissions et troncatures du modèle "
        "surveillées (alertes qualité).":
        "Transkription UND Sprecher in einem einzigen GPU-Durchgang (MOSS), nur für kurze Besprechungen (standardmäßig "
            "10 Min.). Keine Assistenten-Validierung: der direkteste Weg zu einem zugeordneten SRT. Auslassungen und "
            "Kürzungen des Modells werden überwacht (Qualitätswarnungen).",
    "Compte rendu Word présentable rapidement, validation minimale.":
        "Schnell erstelltes, präsentables Word-Protokoll mit minimaler Validierung.",
    "Word template avec participants et structure de réunion, sans correction SRT.":
        "Word-Vorlage mit Teilnehmern und Besprechungsstruktur, ohne SRT-Korrektur.",
    "Word + SRT corrigés (correction LLM), lexique optionnel.": "Korrigiertes Word + SRT (LLM-Korrektur), optionales Lexikon.",
    "Workflow complet : qualité maximale, lexique validé, ZIP complet.":
        "Vollständiger Workflow: maximale Qualität, validiertes Lexikon, vollständiges ZIP.",
    "SRT": "SRT",
    "SRT corrigé": "Korrigiertes SRT",
    "Segments JSON": "JSON-Segmente",
    "Word (template de base)": "Word (Basisvorlage)",
    "Word enrichi": "Erweitertes Word",
    "Word complet": "Vollständiges Word",
    "Rapport qualité complet": "Vollständiger Qualitätsbericht",
    "Archive ZIP complète": "Vollständiges ZIP-Archiv",
    "Résumé de contrôle": "Kontrollzusammenfassung",
    "Contexte de réunion": "Besprechungskontext",
    "Participants": "Teilnehmer",
    "Validation des locuteurs": "Sprechervalidierung",
    "Lexique de session": "Sitzungslexikon",
    "Lexique (optionnel)": "Lexikon (optional)",
    "LLM d'arbitrage non configurée": "Arbitrierungs-LLM nicht konfiguriert",
    "Backend STT 'moss' non activé dans la configuration": "STT-Backend 'moss' in der Konfiguration nicht aktiviert",
    "Mode qualité désactivé dans la configuration": "Qualitätsmodus in der Konfiguration deaktiviert",
    "Profil désactivé dans la configuration": "Profil in der Konfiguration deaktiviert",
}

_ES: dict[str, str] = {
    "SRT express": "SRT exprés",
    "SRT avec locuteurs": "SRT con interlocutores",
    "SRT locuteurs une passe (MOSS)": "SRT con interlocutores en una pasada (MOSS)",
    "Word rapide": "Word rápido",
    "Word structuré": "Word estructurado",
    "Word corrigé": "Word corregido",
    "Dossier qualité complet": "Paquete de calidad completo",
    "Transcription brute, le plus vite possible. Aucune validation.": "Transcripción bruta, lo más rápido posible. Sin validación.",
    "Transcription attribuée aux locuteurs. Validation des locuteurs.":
        "Transcripción atribuida a los interlocutores. Validación de interlocutores.",
    "Transcription ET locuteurs en une seule passe GPU (MOSS), réservée aux réunions courtes (10 min par défaut). "
        "Aucune validation wizard : la voie la plus directe pour un SRT attribué. Omissions et troncatures du modèle "
        "surveillées (alertes qualité).":
        "Transcripción Y interlocutores en una sola pasada de GPU (MOSS), reservada para reuniones cortas (10 min por "
        "defecto). Sin validación del asistente: la vía más directa hacia un SRT atribuido. Se supervisan las omisiones "
        "y truncamientos del modelo (alertas de calidad).",
    "Compte rendu Word présentable rapidement, validation minimale.": "Acta Word presentable con rapidez, validación mínima.",
    "Word template avec participants et structure de réunion, sans correction SRT.":
        "Plantilla Word con participantes y estructura de reunión, sin corrección del SRT.",
    "Word + SRT corrigés (correction LLM), lexique optionnel.": "Word + SRT corregidos (corrección LLM), léxico opcional.",
    "Workflow complet : qualité maximale, lexique validé, ZIP complet.":
        "Flujo de trabajo completo: calidad máxima, léxico validado, ZIP completo.",
    "SRT": "SRT",
    "SRT corrigé": "SRT corregido",
    "Segments JSON": "Segmentos JSON",
    "Word (template de base)": "Word (plantilla básica)",
    "Word enrichi": "Word enriquecido",
    "Word complet": "Word completo",
    "Rapport qualité complet": "Informe de calidad completo",
    "Archive ZIP complète": "Archivo ZIP completo",
    "Résumé de contrôle": "Resumen de control",
    "Contexte de réunion": "Contexto de la reunión",
    "Participants": "Participantes",
    "Validation des locuteurs": "Validación de interlocutores",
    "Lexique de session": "Léxico de la sesión",
    "Lexique (optionnel)": "Léxico (opcional)",
    "LLM d'arbitrage non configurée": "LLM de arbitraje no configurado",
    "Backend STT 'moss' non activé dans la configuration": "Backend STT 'moss' no activado en la configuración",
    "Mode qualité désactivé dans la configuration": "Modo de calidad desactivado en la configuración",
    "Profil désactivé dans la configuration": "Perfil desactivado en la configuración",
}

_IT: dict[str, str] = {
    "SRT express": "SRT express",
    "SRT avec locuteurs": "SRT con parlanti",
    "SRT locuteurs une passe (MOSS)": "SRT con parlanti in un'unica passata (MOSS)",
    "Word rapide": "Word rapido",
    "Word structuré": "Word strutturato",
    "Word corrigé": "Word corretto",
    "Dossier qualité complet": "Pacchetto qualità completo",
    "Transcription brute, le plus vite possible. Aucune validation.":
        "Trascrizione grezza, il più rapidamente possibile. Nessuna convalida.",
    "Transcription attribuée aux locuteurs. Validation des locuteurs.": "Trascrizione attribuita ai parlanti. Convalida dei parlanti.",
    "Transcription ET locuteurs en une seule passe GPU (MOSS), réservée aux réunions courtes (10 min par défaut). "
        "Aucune validation wizard : la voie la plus directe pour un SRT attribué. Omissions et troncatures du modèle "
        "surveillées (alertes qualité).":
        "Trascrizione E parlanti in un'unica passata GPU (MOSS), riservata alle riunioni brevi (10 min di default). "
        "Nessuna convalida guidata: la via più diretta per un SRT attribuito. Omissioni e troncamenti del modello "
        "monitorati (avvisi di qualità).",
    "Compte rendu Word présentable rapidement, validation minimale.": "Verbale Word presentabile rapidamente, convalida minima.",
    "Word template avec participants et structure de réunion, sans correction SRT.":
        "Modello Word con partecipanti e struttura della riunione, senza correzione SRT.",
    "Word + SRT corrigés (correction LLM), lexique optionnel.": "Word + SRT corretti (correzione LLM), lessico opzionale.",
    "Workflow complet : qualité maximale, lexique validé, ZIP complet.":
        "Flusso di lavoro completo: qualità massima, lessico convalidato, ZIP completo.",
    "SRT": "SRT",
    "SRT corrigé": "SRT corretto",
    "Segments JSON": "Segmenti JSON",
    "Word (template de base)": "Word (modello base)",
    "Word enrichi": "Word arricchito",
    "Word complet": "Word completo",
    "Rapport qualité complet": "Rapporto qualità completo",
    "Archive ZIP complète": "Archivio ZIP completo",
    "Résumé de contrôle": "Riepilogo di controllo",
    "Contexte de réunion": "Contesto della riunione",
    "Participants": "Partecipanti",
    "Validation des locuteurs": "Convalida dei parlanti",
    "Lexique de session": "Lessico di sessione",
    "Lexique (optionnel)": "Lessico (opzionale)",
    "LLM d'arbitrage non configurée": "LLM di arbitraggio non configurata",
    "Backend STT 'moss' non activé dans la configuration": "Backend STT 'moss' non attivato nella configurazione",
    "Mode qualité désactivé dans la configuration": "Modalità qualità disattivata nella configurazione",
    "Profil désactivé dans la configuration": "Profilo disattivato nella configurazione",
}

# Tables par langue (même idiome que ``_DOCX_LABELS`` / ``_TYPE_DISPLAY_I18N``) : une
# locale absente retombe sur le FR inchangé — ajouter une langue = ajouter son dict ici.
_TABLES: dict[str, dict[str, str]] = {"en": _EN, "de": _DE, "es": _ES, "it": _IT}


def localize_profile_text(text: str, language: str | None) -> str:
    """Traduit une chaîne d'affichage de profil vers la locale UI (repli = FR inchangé)."""
    table = _TABLES.get(language or "fr")
    return table.get(text, text) if table else text

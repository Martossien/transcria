"""Textes de service de « Discuter avec mes réunions », six langues (clé logique → texte).

Choisis par la LOCALE DE LA REQUÊTE (celle de l'interface au moment de la question), jamais par
la langue d'une réunion : une réponse couvre plusieurs réunions de plusieurs propriétaires.
"""
from __future__ import annotations

_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "internal": "La demande a échoué (erreur interne, détail dans le journal du serveur).",
        "vram": "Mémoire GPU insuffisante pour démarrer la LLM d'arbitrage. Réessayez plus tard.",
        "not_found": "Aucune des réunions que vous pouvez consulter ne permet de répondre à cette question.",
        "disabled": "La discussion avec les réunions est désactivée par l'administrateur.",
        "no_meetings": "Aucune réunion ne vous est visible (ou toutes sont confidentielles et exclues).",
        "busy": "La LLM d'arbitrage est occupée par un traitement en cours. Réessayez dans quelques minutes.",
        "unavailable": "La LLM d'arbitrage n'a pas pu être démarrée. Réessayez plus tard.",
        "timeout": "La demande a expiré avant d'être traitée (LLM occupée trop longtemps).",
        "transcript_unavailable": "La transcription de cette réunion n'est pas disponible.",
        "target_not_visible": "Cette réunion ne fait pas partie de celles que vous pouvez consulter.",
        "failed": "La demande a échoué : {reason}",
        "cloud_refused": "Demande refusée : {reason}",
        "level_1": "d'après les fiches des réunions",
        "level_2": "d'après la synthèse des réunions les plus proches",
        "level_3": "d'après la transcription de la réunion",
    },
    "en": {
        "internal": "The request failed (internal error, details in the server log).",
        "vram": "Not enough GPU memory to start the arbitration LLM. Try again later.",
        "not_found": "None of the meetings you can access answers this question.",
        "disabled": "Chatting with meetings has been disabled by the administrator.",
        "no_meetings": "No meeting is visible to you (or all of them are confidential and excluded).",
        "busy": "The arbitration LLM is busy with a running job. Try again in a few minutes.",
        "unavailable": "The arbitration LLM could not be started. Try again later.",
        "timeout": "The request expired before being processed (LLM busy for too long).",
        "transcript_unavailable": "The transcript of this meeting is not available.",
        "target_not_visible": "This meeting is not among those you can access.",
        "failed": "The request failed: {reason}",
        "cloud_refused": "Request refused: {reason}",
        "level_1": "based on the meeting cards",
        "level_2": "based on the summaries of the closest meetings",
        "level_3": "based on the meeting transcript",
    },
    "de": {
        "internal": "Die Anfrage ist fehlgeschlagen (interner Fehler, Details im Serverprotokoll).",
        "vram": "Nicht genug GPU-Speicher, um das Schieds-LLM zu starten. Versuchen Sie es später erneut.",
        "not_found": "Keine der Besprechungen, die Sie einsehen können, beantwortet diese Frage.",
        "disabled": "Das Gespräch mit den Besprechungen wurde vom Administrator deaktiviert.",
        "no_meetings": "Keine Besprechung ist für Sie sichtbar (oder alle sind vertraulich und ausgeschlossen).",
        "busy": "Das Schieds-LLM ist durch eine laufende Verarbeitung belegt. Versuchen Sie es in einigen Minuten erneut.",
        "unavailable": "Das Schieds-LLM konnte nicht gestartet werden. Versuchen Sie es später erneut.",
        "timeout": "Die Anfrage ist abgelaufen, bevor sie bearbeitet wurde (LLM zu lange belegt).",
        "transcript_unavailable": "Die Transkription dieser Besprechung ist nicht verfügbar.",
        "target_not_visible": "Diese Besprechung gehört nicht zu denen, die Sie einsehen können.",
        "failed": "Die Anfrage ist fehlgeschlagen: {reason}",
        "cloud_refused": "Anfrage abgelehnt: {reason}",
        "level_1": "anhand der Besprechungskarten",
        "level_2": "anhand der Zusammenfassungen der nächstliegenden Besprechungen",
        "level_3": "anhand der Transkription der Besprechung",
    },
    "es": {
        "internal": "La solicitud ha fallado (error interno, detalles en el registro del servidor).",
        "vram": "Memoria GPU insuficiente para iniciar la LLM de arbitraje. Inténtelo más tarde.",
        "not_found": "Ninguna de las reuniones que puede consultar responde a esta pregunta.",
        "disabled": "El administrador ha desactivado la conversación con las reuniones.",
        "no_meetings": "No tiene ninguna reunión visible (o todas son confidenciales y están excluidas).",
        "busy": "La LLM de arbitraje está ocupada con un procesamiento en curso. Inténtelo de nuevo en unos minutos.",
        "unavailable": "No se pudo iniciar la LLM de arbitraje. Inténtelo más tarde.",
        "timeout": "La solicitud caducó antes de ser procesada (LLM ocupada demasiado tiempo).",
        "transcript_unavailable": "La transcripción de esta reunión no está disponible.",
        "target_not_visible": "Esta reunión no está entre las que puede consultar.",
        "failed": "La solicitud ha fallado: {reason}",
        "cloud_refused": "Solicitud rechazada: {reason}",
        "level_1": "según las fichas de las reuniones",
        "level_2": "según las síntesis de las reuniones más cercanas",
        "level_3": "según la transcripción de la reunión",
    },
    "it": {
        "internal": "La richiesta non è riuscita (errore interno, dettagli nel registro del server).",
        "vram": "Memoria GPU insufficiente per avviare la LLM di arbitraggio. Riprovi più tardi.",
        "not_found": "Nessuna delle riunioni che può consultare risponde a questa domanda.",
        "disabled": "La conversazione con le riunioni è stata disattivata dall'amministratore.",
        "no_meetings": "Nessuna riunione è visibile per lei (oppure sono tutte riservate ed escluse).",
        "busy": "La LLM di arbitraggio è occupata da un'elaborazione in corso. Riprovi tra qualche minuto.",
        "unavailable": "La LLM di arbitraggio non è stata avviata. Riprovi più tardi.",
        "timeout": "La richiesta è scaduta prima di essere elaborata (LLM occupata troppo a lungo).",
        "transcript_unavailable": "La trascrizione di questa riunione non è disponibile.",
        "target_not_visible": "Questa riunione non fa parte di quelle che può consultare.",
        "failed": "La richiesta non è riuscita: {reason}",
        "cloud_refused": "Richiesta rifiutata: {reason}",
        "level_1": "in base alle schede delle riunioni",
        "level_2": "in base alle sintesi delle riunioni più vicine",
        "level_3": "in base alla trascrizione della riunione",
    },
    "pt": {
        "internal": "O pedido falhou (erro interno, detalhes no registo do servidor).",
        "vram": "Memória GPU insuficiente para iniciar a LLM de arbitragem. Tente novamente mais tarde.",
        "not_found": "Nenhuma das reuniões que pode consultar responde a esta pergunta.",
        "disabled": "A conversa com as reuniões foi desativada pelo administrador.",
        "no_meetings": "Nenhuma reunião está visível para si (ou todas são confidenciais e excluídas).",
        "busy": "A LLM de arbitragem está ocupada com um processamento em curso. Tente novamente dentro de alguns minutos.",
        "unavailable": "Não foi possível iniciar a LLM de arbitragem. Tente novamente mais tarde.",
        "timeout": "O pedido expirou antes de ser tratado (LLM ocupada demasiado tempo).",
        "transcript_unavailable": "A transcrição desta reunião não está disponível.",
        "target_not_visible": "Esta reunião não faz parte das que pode consultar.",
        "failed": "O pedido falhou: {reason}",
        "cloud_refused": "Pedido recusado: {reason}",
        "level_1": "com base nas fichas das reuniões",
        "level_2": "com base nas sínteses das reuniões mais próximas",
        "level_3": "com base na transcrição da reunião",
    },
}


def ask_message(locale: str | None, key: str, **fmt: object) -> str:
    """Texte ``key`` dans ``locale`` (repli français), formaté."""
    table = _MESSAGES.get((locale or "fr").split("-")[0].lower(), _MESSAGES["fr"])
    text = table.get(key) or _MESSAGES["fr"].get(key, key)
    return text.format(**fmt) if fmt else text


SUPPORTED_LOCALES = tuple(_MESSAGES)

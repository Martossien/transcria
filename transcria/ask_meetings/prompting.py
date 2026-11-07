"""Rendu des réunions pour la LLM et garde des citations — PUR (docs/DISCUTER_REUNIONS.md § 2.2, § 2.6).

Les réunions sont numérotées ``[1]``, ``[2]``… dans le prompt (jamais des identifiants : une
petite LLM locale les recopie mal). La LLM cite par numéro ; ``verify_citations`` ne garde que les
numéros de la liste envoyée et retire tout le reste.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from transcria.search.preselect import CardDoc

_LABELS: dict[str, dict[str, str]] = {
    "fr": {"speaking": "Temps de parole", "unnamed": "locuteurs non nommés",
          "app_name": "Nom dans l'application",
           "material": "Matière fournie", "level_cards": "fiches seules", "level_summaries": "fiches et synthèses",
           "level_transcript": "fiche et transcription d'une réunion",
           "type": "Type", "date": "Date", "import_date": "date d'import", "participants": "Participants",
           "topic": "Sujet", "objective": "Objectif", "agenda": "Ordre du jour", "abstract": "Résumé",
           "decisions": "Décisions", "actions": "Actions", "next_date": "Prochaine échéance",
           "summary": "Synthèse", "transcript": "Transcription", "untitled": "Réunion sans titre",
           "meetings": "Réunions consultables", "truncated": "(transcription tronquée : {pct} % montrés)"},
    "en": {"speaking": "Speaking time", "unnamed": "unnamed speakers",
          "app_name": "Name in the application",
           "material": "Material provided", "level_cards": "cards only", "level_summaries": "cards and summaries",
           "level_transcript": "card and transcript of one meeting",
           "type": "Type", "date": "Date", "import_date": "import date", "participants": "Participants",
           "topic": "Topic", "objective": "Objective", "agenda": "Agenda", "abstract": "Abstract",
           "decisions": "Decisions", "actions": "Actions", "next_date": "Next deadline",
           "summary": "Summary", "transcript": "Transcript", "untitled": "Untitled meeting",
           "meetings": "Meetings you can consult", "truncated": "(transcript truncated: {pct}% shown)"},
    "de": {"speaking": "Redezeit", "unnamed": "nicht benannte Sprecher",
          "app_name": "Name in der Anwendung",
           "material": "Bereitgestelltes Material", "level_cards": "nur Karten", "level_summaries": "Karten und Zusammenfassungen",
           "level_transcript": "Karte und Transkription einer Besprechung",
           "type": "Typ", "date": "Datum", "import_date": "Importdatum", "participants": "Teilnehmer",
           "topic": "Thema", "objective": "Ziel", "agenda": "Tagesordnung", "abstract": "Kurzfassung",
           "decisions": "Entscheidungen", "actions": "Aufgaben", "next_date": "Nächster Termin",
           "summary": "Zusammenfassung", "transcript": "Transkription", "untitled": "Besprechung ohne Titel",
           "meetings": "Einsehbare Besprechungen", "truncated": "(Transkription gekürzt: {pct} % gezeigt)"},
    "es": {"speaking": "Tiempo de palabra", "unnamed": "oradores sin nombre",
          "app_name": "Nombre en la aplicación",
           "material": "Material proporcionado", "level_cards": "solo fichas", "level_summaries": "fichas y síntesis",
           "level_transcript": "ficha y transcripción de una reunión",
           "type": "Tipo", "date": "Fecha", "import_date": "fecha de importación", "participants": "Participantes",
           "topic": "Tema", "objective": "Objetivo", "agenda": "Orden del día", "abstract": "Resumen",
           "decisions": "Decisiones", "actions": "Acciones", "next_date": "Próximo plazo",
           "summary": "Síntesis", "transcript": "Transcripción", "untitled": "Reunión sin título",
           "meetings": "Reuniones consultables", "truncated": "(transcripción truncada: {pct} % mostrado)"},
    "it": {"speaking": "Tempo di parola", "unnamed": "parlanti non nominati",
          "app_name": "Nome nell'applicazione",
           "material": "Materiale fornito", "level_cards": "solo schede", "level_summaries": "schede e sintesi",
           "level_transcript": "scheda e trascrizione di una riunione",
           "type": "Tipo", "date": "Data", "import_date": "data di importazione", "participants": "Partecipanti",
           "topic": "Argomento", "objective": "Obiettivo", "agenda": "Ordine del giorno", "abstract": "Riassunto",
           "decisions": "Decisioni", "actions": "Azioni", "next_date": "Prossima scadenza",
           "summary": "Sintesi", "transcript": "Trascrizione", "untitled": "Riunione senza titolo",
           "meetings": "Riunioni consultabili", "truncated": "(trascrizione troncata: {pct} % mostrato)"},
    "pt": {"speaking": "Tempo de palavra", "unnamed": "oradores não nomeados",
          "app_name": "Nome na aplicação",
           "material": "Material fornecido", "level_cards": "apenas fichas", "level_summaries": "fichas e sínteses",
           "level_transcript": "ficha e transcrição de uma reunião",
           "type": "Tipo", "date": "Data", "import_date": "data de importação", "participants": "Participantes",
           "topic": "Tema", "objective": "Objetivo", "agenda": "Ordem do dia", "abstract": "Resumo",
           "decisions": "Decisões", "actions": "Ações", "next_date": "Próximo prazo",
           "summary": "Síntese", "transcript": "Transcrição", "untitled": "Reunião sem título",
           "meetings": "Reuniões consultáveis", "truncated": "(transcrição truncada: {pct} % mostrados)"},
}

_CITATION_RE = re.compile(r"\[\s*(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\s*\]")


def labels(locale: str | None) -> dict[str, str]:
    return _LABELS.get((locale or "fr").split("-")[0].lower(), _LABELS["fr"])


def card_doc(job_id: str, job_title: str, card, *, locale: str = "fr") -> CardDoc:
    """``CardDoc`` de présélection depuis une fiche (+ titre du job), avec la taille de son rendu."""
    doc = CardDoc(
        job_id=job_id,
        title=str(card.title or ""),
        job_title=str(job_title or ""),
        meeting_type=str(card.meeting_type or ""),
        meeting_date=card.meeting_date,
        participants=tuple(f"{p.get('name', '')} {p.get('function', '')}".strip() for p in card.participants),
        speaking=tuple(f"{p.get('name', '')} {int(p.get('pct') or 0)} %" for p in getattr(card, "speaking", []) or []),
        unnamed_speech_pct=int(getattr(card, "unnamed_speech_pct", 0) or 0),
        topic=str(card.topic or ""),
        objective=str(card.objective or ""),
        agenda=str(card.agenda or ""),
        abstract=str(card.abstract or ""),
        decisions=tuple(card.decisions),
        actions=tuple(card.actions),
        next_date=str(card.next_date or ""),
    )
    rendered = render_card(1, doc, date_is_import_fallback=bool(card.date_is_import_fallback), locale=locale)
    return CardDoc(**{**doc.__dict__, "rendered_chars": len(rendered)})


def _fmt_date(value: date | None, fallback: bool, lab: dict[str, str]) -> str:
    if value is None:
        return "?"
    text = value.isoformat()
    return f"{text} ({lab['import_date']})" if fallback else text


def render_card(index: int, doc: CardDoc, *, date_is_import_fallback: bool, locale: str = "fr") -> str:
    """Une fiche numérotée, lisible par la LLM. Les listes sont rendues en puces."""
    lab = labels(locale)
    title = doc.title or doc.job_title or lab["untitled"]
    lines = [f"[{index}] {title}",
             f"{lab['type']}: {doc.meeting_type or '?'} — {lab['date']}: {_fmt_date(doc.meeting_date, date_is_import_fallback, lab)}"]
    # Le nom du traitement (celui de l'accueil) diffère souvent du titre saisi à l'étape 4 : un
    # utilisateur demande « la réunion show me the code » par ce nom-là — la LLM doit le voir.
    if doc.job_title and doc.title and doc.job_title.casefold().strip() != doc.title.casefold().strip():
        lines.append(f"{lab['app_name']}: {doc.job_title}")
    if doc.participants:
        lines.append(f"{lab['participants']}: " + "; ".join(doc.participants))
    if doc.speaking:
        shares = ", ".join(doc.speaking)
        if doc.unnamed_speech_pct:
            shares += f" ({lab['unnamed']} : {doc.unnamed_speech_pct} %)"
        lines.append(f"{lab['speaking']}: {shares}")
    for key in ("topic", "objective", "agenda"):
        value = getattr(doc, key)
        if value:
            lines.append(f"{lab[key]}: {value}")
    if doc.abstract:
        lines.append(f"{lab['abstract']}: {doc.abstract}")
    for key in ("decisions", "actions"):
        items = getattr(doc, key)
        if items:
            lines.append(f"{lab[key]}:")
            lines.extend(f"- {item}" for item in items)
    if doc.next_date:
        lines.append(f"{lab['next_date']}: {doc.next_date}")
    return "\n".join(lines)


@dataclass(frozen=True)
class NumberedMeeting:
    """Une réunion telle qu'envoyée à la LLM : son numéro et son identifiant réel."""

    index: int
    job_id: str
    doc: CardDoc
    date_is_import_fallback: bool


def _header(lab: dict[str, str], level_key: str) -> str:
    """La LLM sait ce qu'elle a sous les yeux : fiches seules, synthèses, ou transcription — le
    prompt lui dit quoi faire à chaque niveau (avec les fiches seules, « non retrouvé » suffit)."""
    return f"## {lab['meetings']}\n{lab['material']} : {lab[level_key]}.\n"


def render_cards_block(meetings: list[NumberedMeeting], *, locale: str = "fr") -> str:
    lab = labels(locale)
    parts = [_header(lab, "level_cards")]
    parts.extend(render_card(m.index, m.doc, date_is_import_fallback=m.date_is_import_fallback, locale=locale)
                 for m in meetings)
    return "\n\n".join(parts)


def render_summaries_block(meetings: list[NumberedMeeting], summaries: dict[str, str], *, locale: str = "fr") -> str:
    """Niveau 2 : fiche + synthèse effective de chaque réunion retenue."""
    lab = labels(locale)
    parts = [_header(lab, "level_summaries")]
    for m in meetings:
        card = render_card(m.index, m.doc, date_is_import_fallback=m.date_is_import_fallback, locale=locale)
        summary = (summaries.get(m.job_id) or "").strip()
        parts.append(f"{card}\n\n### {lab['summary']} [{m.index}]\n{summary or '(vide)'}")
    return "\n\n".join(parts)


def render_transcript_block(meeting: NumberedMeeting, transcript: str, *, shown_pct: int | None,
                            locale: str = "fr") -> str:
    """Niveau 3 : la fiche et la transcription d'UNE réunion."""
    lab = labels(locale)
    card = render_card(meeting.index, meeting.doc, date_is_import_fallback=meeting.date_is_import_fallback,
                       locale=locale)
    note = f"\n{lab['truncated'].format(pct=shown_pct)}" if shown_pct is not None else ""
    return (f"{_header(lab, 'level_transcript')}\n{card}\n\n### {lab['transcript']} [{meeting.index}]{note}\n"
            f"{transcript.strip() or '(vide)'}")


def build_messages(*, system_prompt: str, context_block: str, history: list[dict], question: str) -> list[dict]:
    """Messages OpenAI-chat : système = contrat + réunions inline, historique en vrais tours,
    puis la question. Un tour vide ou d'un autre rôle n'est pas rejoué."""
    messages: list[dict] = [{"role": "system", "content": f"{system_prompt.strip()}\n\n{context_block.strip()}\n"}]
    for turn in history:
        role = turn.get("role")
        text = str(turn.get("text") or "").strip()
        if text and role in ("user", "assistant"):
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": question})
    return messages


def verify_citations(answer: str, allowed: int) -> tuple[str, list[int]]:
    """Garde des citations : ne garde que les numéros ``1..allowed`` ; les autres sont RETIRÉS
    du texte. Rend le texte nettoyé et les numéros cités (uniques, ordre d'apparition)."""
    cited: list[int] = []

    def _replace(match: re.Match) -> str:
        numbers = [int(n) for n in re.split(r"\s*[,;]\s*", match.group(1))]
        valid = [n for n in numbers if 1 <= n <= allowed]
        for n in valid:
            if n not in cited:
                cited.append(n)
        return "[" + ", ".join(str(n) for n in valid) + "]" if valid else ""

    cleaned = _CITATION_RE.sub(_replace, answer or "")
    cleaned = re.sub(r"[:;,]\s*([.!?])", r"\1", cleaned)           # « Hors liste : . » → « Hors liste . »
    # Seuls « , » et « . » sont recollés : « ? », « ! », « : », « ; » prennent une espace en français.
    cleaned = re.sub(r"[ \t]+([,.])", r"\1", cleaned)             # « validé [9]. » → « validé. »
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    return cleaned, cited


def strip_citations(text: str) -> str:
    """Retire TOUTES les citations ``[n]`` (historique rejoué : les numéros d'un tour précédent ne
    désignent pas les réunions de la liste actuelle)."""
    return verify_citations(text, 0)[0]

"""Classement des fiches visibles pour une question (PURE — docs/DISCUTER_REUNIONS.md § 2.4).

Score par champ pondéré (titre, sujet, participants… ), bonus de date quand la question en
contient une, puis les meilleures sous un double plafond (nombre de fiches, tokens estimés).
Les fiches sont fournies par l'appelant DÉJÀ filtrées par la règle de visibilité et les
exclusions (confidentiel, cloud) : ce module ne sait pas qui demande.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from transcria.search.text import STOPWORDS, fold, same_word, tokens

#: Poids des champs. Le titre et le sujet nomment la réunion ; les participants répondent à
#: « la réunion avec X » ; l'abrégé est long et bruité, il pèse peu.
FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "job_title": 3.0,
    "topic": 2.5,
    "participants": 2.5,
    "objective": 1.5,
    "agenda": 1.5,
    "decisions": 1.5,
    "actions": 1.5,
    "meeting_type": 1.0,
    "abstract": 1.0,
}
DATE_BONUS_SAME_DAY = 4.0
DATE_BONUS_SAME_MONTH = 2.0
#: Estimation de tokens : ≈ 4 caractères par token (mesuré sur les fiches réelles, lot 0).
CHARS_PER_TOKEN = 4

_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "marz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "settembre": 9,
    "ottobre": 10, "dicembre": 12,
    "janeiro": 1, "fevereiro": 2, "marco": 3, "maio": 5, "junho": 6, "julho": 7, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")
_DAY_MONTH_YEAR = re.compile(r"(?<!\d)(\d{1,2})(?:er)?\s+(?:de\s+)?([a-z]{3,10})\.?\s+(?:de\s+)?(\d{4})(?!\d)")
_MONTH_YEAR = re.compile(r"(?<![a-z])([a-z]{3,10})\.?\s+(?:de\s+)?(\d{4})(?!\d)")


@dataclass(frozen=True)
class CardDoc:
    """Ce que la présélection lit d'une fiche (projection de ``JobCard`` + titre du job)."""

    job_id: str
    title: str = ""
    job_title: str = ""
    meeting_type: str = ""
    meeting_date: date | None = None
    participants: tuple[str, ...] = ()
    topic: str = ""
    objective: str = ""
    agenda: str = ""
    abstract: str = ""
    decisions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    next_date: str = ""
    #: Taille du rendu de la fiche dans le prompt (caractères) — fournie par l'appelant qui rend.
    rendered_chars: int = 0

    @property
    def tokens_estimate(self) -> int:
        return max(1, self.rendered_chars // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class DateHint:
    """Date lue dans la question : jour précis, ou mois seul."""

    year: int
    month: int
    day: int | None = None


@dataclass
class Ranked:
    doc: CardDoc
    score: float
    matched: list[str] = field(default_factory=list)


def _month_number(token: str) -> int | None:
    if token in _MONTHS:
        return _MONTHS[token]
    hits = {n for name, n in _MONTHS.items() if name.startswith(token) and len(token) >= 3}
    return hits.pop() if len(hits) == 1 else None


def _valid(year: int, month: int, day: int | None) -> DateHint | None:
    try:
        date(year, month, day or 1)
    except ValueError:
        return None
    if not 1990 <= year <= 2100:
        return None
    return DateHint(year, month, day)


def date_hints(question: str) -> list[DateHint]:
    """Dates (jour ou mois+année) mentionnées dans la question."""
    text = fold(question)
    hints: list[DateHint] = []
    for m in _ISO.finditer(text):
        hint = _valid(int(m[1]), int(m[2]), int(m[3]))
        if hint:
            hints.append(hint)
    for m in _NUMERIC.finditer(text):
        hint = _valid(int(m[3]), int(m[2]), int(m[1]))
        if hint:
            hints.append(hint)
    for m in _DAY_MONTH_YEAR.finditer(text):
        month = _month_number(m[2])
        if month:
            hint = _valid(int(m[3]), month, int(m[1]))
            if hint:
                hints.append(hint)
    covered = {(h.year, h.month) for h in hints}
    for m in _MONTH_YEAR.finditer(text):
        month = _month_number(m[1])
        if month and (int(m[2]), month) not in covered:
            hint = _valid(int(m[2]), month, None)
            if hint:
                hints.append(hint)
    return hints


def question_tokens(question: str) -> list[str]:
    """Jetons utiles de la question, dédoublonnés, mots-outils retirés."""
    return list(dict.fromkeys(tokens(question)))


def _field_tokens(doc: CardDoc) -> dict[str, set[str]]:
    return {
        "title": set(tokens(doc.title)),
        "job_title": set(tokens(doc.job_title)),
        "topic": set(tokens(doc.topic)),
        "participants": set(tokens(" ".join(doc.participants))),
        "objective": set(tokens(doc.objective)),
        "agenda": set(tokens(doc.agenda)),
        "decisions": set(tokens(" ".join(doc.decisions))),
        "actions": set(tokens(" ".join(doc.actions))),
        "meeting_type": set(tokens(doc.meeting_type)),
        "abstract": set(tokens(doc.abstract)),
    }


def score_doc(doc: CardDoc, q_tokens: list[str], hints: list[DateHint]) -> Ranked:
    """Score d'une fiche : somme des poids des champs où chaque jeton de la question apparaît
    (un jeton compte une fois par champ), plus le bonus de date."""
    fields = _field_tokens(doc)
    score = 0.0
    matched: list[str] = []
    for token in q_tokens:
        hit = False
        for name, weight in FIELD_WEIGHTS.items():
            if any(same_word(token, t) for t in fields[name]):
                score += weight
                hit = True
        if hit:
            matched.append(token)
    if doc.meeting_date is not None:
        for hint in hints:
            if (doc.meeting_date.year, doc.meeting_date.month) != (hint.year, hint.month):
                continue
            if hint.day is None:
                score += DATE_BONUS_SAME_MONTH
            elif doc.meeting_date.day == hint.day:
                score += DATE_BONUS_SAME_DAY
    return Ranked(doc=doc, score=score, matched=matched)


def rank(docs: list[CardDoc], question: str) -> list[Ranked]:
    """Toutes les fiches classées : score décroissant, puis date décroissante (la plus récente
    d'abord à score égal — et donc en tête quand la question ne cible rien de précis)."""
    q_tokens = question_tokens(question)
    hints = date_hints(question)
    ranked = [score_doc(doc, q_tokens, hints) for doc in docs]
    ranked.sort(key=lambda r: (-r.score, -(r.doc.meeting_date.toordinal() if r.doc.meeting_date else 0)))
    return ranked


def preselect(docs: list[CardDoc], question: str, *, max_cards: int, max_tokens: int) -> list[Ranked]:
    """Les meilleures fiches sous le double plafond.

    Quand aucune fiche n'obtient de score (question sans mot discriminant : « qu'a-t-on décidé
    récemment ? »), les plus récentes sont envoyées — jamais une liste vide tant qu'il y a des
    fiches visibles : la LLM dira elle-même « non retrouvé » s'il le faut.
    """
    kept: list[Ranked] = []
    budget = max_tokens
    for item in rank(docs, question):
        if len(kept) >= max_cards:
            break
        if item.doc.tokens_estimate > budget:
            continue
        kept.append(item)
        budget -= item.doc.tokens_estimate
    return kept


__all__ = ["CardDoc", "DateHint", "Ranked", "STOPWORDS", "date_hints", "preselect", "question_tokens",
           "rank", "score_doc"]

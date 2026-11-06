"""Vue « Mes actions » — PURE (docs/DISCUTER_REUNIONS.md § 2.9) : les lignes ``actions`` des fiches
visibles, groupées par réunion ou par responsable. Aucune LLM, aucun statut, aucune case à cocher.

Le responsable est le texte avant le premier « : » SUIVI D'UN ESPACE (« Personne A : envoyer le
devis (vendredi) ») — « 14:00 » n'est pas un responsable. Mesuré sur l'installation de dev :
358 actions sur 412 portent cette forme.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_OWNER_RE = re.compile(r"^\s*([^:\n]{1,80}?)\s*:\s+(.+)$", re.S)
UNASSIGNED = ""


@dataclass(frozen=True)
class ActionLine:
    job_id: str
    meeting_title: str
    meeting_date: date | None
    date_is_import_fallback: bool
    meeting_type: str
    owner: str          # "" = non attribué
    text: str           # l'action sans le préfixe responsable
    raw: str


@dataclass
class ActionGroup:
    key: str            # job_id ou responsable (""= non attribué)
    label: str
    lines: list[ActionLine]


def split_owner(action: str) -> tuple[str, str]:
    """``("Personne A", "envoyer le devis (vendredi)")`` ou ``("", action)``."""
    match = _OWNER_RE.match(action or "")
    if not match:
        return UNASSIGNED, (action or "").strip()
    owner = match.group(1).strip()
    if any(ch.isdigit() for ch in owner) and len(owner) <= 5:      # « 14:00 », « 9h30 : » …
        return UNASSIGNED, (action or "").strip()
    return owner, match.group(2).strip()


def action_lines(candidates, *, meeting_type: str = "", date_from: date | None = None,
                 date_to: date | None = None) -> list[ActionLine]:
    """Lignes d'action des fiches, filtrées par type et fenêtre de dates (bornes incluses)."""
    lines: list[ActionLine] = []
    for cand in candidates:
        card, job = cand.card, cand.job
        if meeting_type and (card.meeting_type or "") != meeting_type:
            continue
        when = card.meeting_date
        if date_from and (when is None or when < date_from):
            continue
        if date_to and (when is None or when > date_to):
            continue
        for raw in card.actions:
            owner, text = split_owner(raw)
            lines.append(ActionLine(
                job_id=job.id, meeting_title=card.title or job.title, meeting_date=when,
                date_is_import_fallback=bool(card.date_is_import_fallback), meeting_type=card.meeting_type or "",
                owner=owner, text=text, raw=raw))
    return lines


def group_by_meeting(lines: list[ActionLine]) -> list[ActionGroup]:
    """Une entrée par réunion, de la plus récente à la plus ancienne (ordre des lignes conservé)."""
    groups: dict[str, ActionGroup] = {}
    for line in lines:
        group = groups.get(line.job_id)
        if group is None:
            group = groups[line.job_id] = ActionGroup(key=line.job_id, label=line.meeting_title, lines=[])
        group.lines.append(line)
    def _when(g: ActionGroup) -> int:
        d = g.lines[0].meeting_date
        return d.toordinal() if d else 0
    return sorted(groups.values(), key=_when, reverse=True)


def group_by_owner(lines: list[ActionLine]) -> list[ActionGroup]:
    """Une entrée par responsable (casse et accents ignorés pour regrouper), « non attribué » en
    dernier ; dans chaque groupe, la réunion la plus récente d'abord."""
    import unicodedata

    def _key(owner: str) -> str:
        return "".join(ch for ch in unicodedata.normalize("NFD", owner) if unicodedata.category(ch) != "Mn").casefold().strip()

    groups: dict[str, ActionGroup] = {}
    for line in lines:
        key = _key(line.owner)
        group = groups.get(key)
        if group is None:
            group = groups[key] = ActionGroup(key=key, label=line.owner, lines=[])
        elif line.owner[:1].isupper() and not group.label[:1].isupper():
            group.label = line.owner           # « personne a » et « Personne A » : la graphie capitalisée s'affiche
        group.lines.append(line)
    ordered = sorted((g for k, g in groups.items() if k), key=lambda g: g.key)
    for group in ordered:
        group.lines.sort(key=lambda ln: ln.meeting_date.toordinal() if ln.meeting_date else 0, reverse=True)
    if "" in groups:
        unassigned = groups[""]
        unassigned.lines.sort(key=lambda ln: ln.meeting_date.toordinal() if ln.meeting_date else 0, reverse=True)
        ordered.append(unassigned)
    return ordered


def meeting_types_present(candidates) -> list[str]:
    """Types présents dans les fiches (pour le filtre), triés, sans vide."""
    return sorted({str(c.card.meeting_type or "") for c in candidates if c.card.meeting_type})

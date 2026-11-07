"""Projection PURE d'un contexte de réunion en fiche courte (``docs/DISCUTER_REUNIONS.md`` § 2.3).

Aucune base, aucun disque : l'appelant fournit le contenu des deux fichiers de contexte et ce
que la fiche ne peut pas deviner (date de création du job, types confidentiels du catalogue,
marqueurs de la section synthèse). Tout ce qui entre est du contenu utilisateur ou LLM, donc de
forme incertaine : chaque champ est borné et retombe sur une valeur vide plutôt que d'échouer.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from transcria.context.meeting_context import synthese_section
from transcria.context.participants import is_label

#: Version de la projection. Entre dans l'empreinte : la relever force le recalcul de toutes les
#: fiches au prochain rattrapage (règle modifiée ⇒ fiches périmées, même si les fichiers n'ont pas bougé).
CARD_VERSION = 2

ABSTRACT_MAX_CHARS = 800
MAX_LIST_ITEMS = 40
MAX_ITEM_CHARS = 400
MAX_PARTICIPANTS = 60
TOPIC_MAX_CHARS = 400
OBJECTIVE_MAX_CHARS = 300
AGENDA_MAX_CHARS = 700
_TITLE_MAX_CHARS = 255
_NEXT_DATE_MAX_CHARS = 120

#: Valeurs de ``sensitivity`` qui ne marquent PAS une réunion comme confidentielle.
_NORMAL_SENSITIVITY = frozenset({"", "normal"})

_MONTHS_FR = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?!\d)")
_NAMED_DATE = re.compile(r"(?<!\d)(\d{1,2})(?:er)?\s+([a-z]{3,9})\.?\s+(\d{4})(?!\d)")


@dataclass(frozen=True)
class CardData:
    """Le contenu d'une fiche, tel que la table le stocke."""

    title: str
    meeting_type: str
    meeting_date: date | None
    date_is_import_fallback: bool
    confidential: bool
    sensitivity: str
    language: str
    participants: list[dict]
    #: Parts du temps de parole des personnes NOMMÉES (``{"name", "pct"}``, décroissant) et part
    #: restante des locuteurs non nommés — « qui parle le plus de… » (retour utilisateurs 2026-11).
    speaking: list[dict]
    unnamed_speech_pct: int
    topic: str
    objective: str
    agenda: str
    abstract: str
    decisions: list[str]
    actions: list[str]
    next_date: str


def fold(text: str) -> str:
    """Minuscules sans accents (NFD + casefold) — comparaison tolérante de libellés."""
    decomposed = unicodedata.normalize("NFD", str(text or ""))
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").casefold().strip()


def _safe_date(year: int, month: int, day: int) -> date | None:
    if not 1990 <= year <= 2100:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_meeting_date(raw: object) -> date | None:
    """Date de réunion depuis la chaîne LIBRE du contexte, ou ``None`` si elle n'est pas analysable.

    Le formulaire de l'étape 4 envoie de l'ISO (``<input type="date">``), une réunion planifiée
    sème ``%Y-%m-%d`` ; l'API et les anciens jobs peuvent porter une saisie libre. Reconnus : ISO
    (suivi ou non d'une heure), ``JJ/MM/AAAA`` (séparateurs ``/ . -``, jour en premier — sauf si
    seul l'ordre mois/jour est possible), ``12 mai 2025`` (mois français, « 1er » accepté).
    Une date impossible (31 février, année hors 1990-2100) n'est pas corrigée : ``None``.
    """
    if not isinstance(raw, str):
        return None
    text = fold(raw)
    if not text:
        return None
    match = _ISO_DATE.search(text)
    if match:
        return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    match = _NUMERIC_DATE.search(text)
    if match:
        day, month, year = int(match[1]), int(match[2]), int(match[3])
        if year < 100:
            year += 2000
        if month > 12 >= day:
            day, month = month, day
        return _safe_date(year, month, day)
    match = _NAMED_DATE.search(text)
    if match:
        named_month = _month_number(match[2])
        if named_month:
            return _safe_date(int(match[3]), named_month, int(match[1]))
    return None


def _month_number(token: str) -> int | None:
    """Mois français depuis son nom ou une abréviation NON ambiguë (« janv », « sept » ; pas « jui »)."""
    if token in _MONTHS_FR:
        return _MONTHS_FR[token]
    candidates = {number for name, number in _MONTHS_FR.items() if name.startswith(token)}
    return candidates.pop() if len(candidates) == 1 else None


def type_is_validated(meeting_ctx: dict) -> bool:
    """L'étape 4 a-t-elle été enregistrée ?

    Seul ``MeetingContextManager.save()`` (la route de l'étape 4) fusionne les défauts, donc pose la
    clé ``meeting_type``. Les écritures du worker et le semis d'une réunion planifiée ne la posent
    jamais. Critère lu dans le FICHIER, pas dans l'état du job : la fiche est recalculée au moment
    de l'écriture, avant que l'état ne passe à « contexte validé ».
    """
    return "meeting_type" in meeting_ctx


def is_confidential(meeting_ctx: dict, *, confidential_types: frozenset[str],
                    known_types: frozenset[str]) -> bool:
    """Règle « confidentiel » d'une fiche (§ 2.3) — dans le doute, confidentiel.

    Vrai si : ``sensitivity`` ≠ normal, OU type choisi confidentiel (intégré), OU type personnalisé
    matérialisé avec ``behavior.confidential``, OU — tant que l'étape 4 n'est pas enregistrée — type
    SUGGÉRÉ qui n'est pas un type intégré non confidentiel. Ce dernier cas couvre le type suggéré
    confidentiel ET le type suggéré inconnu (personnalisé non résolu, libellé décoré par la LLM) :
    on ne peut pas prouver qu'il n'est pas confidentiel, il est donc exclu par défaut.
    """
    if fold(meeting_ctx.get("sensitivity") or "") not in _NORMAL_SENSITIVITY:
        return True
    confidential = {fold(name) for name in confidential_types}
    if fold(meeting_ctx.get("meeting_type") or "") in confidential:
        return True
    custom = meeting_ctx.get("custom_type")
    if isinstance(custom, dict):
        behavior = custom.get("behavior")
        if isinstance(behavior, dict) and behavior.get("confidential"):
            return True
    if type_is_validated(meeting_ctx):
        return False
    suggested = fold(meeting_ctx.get("type_suggere") or "")
    if not suggested:
        return False
    harmless = {fold(name) for name in known_types} - confidential
    return suggested not in harmless


def effective_synthesis(meeting_ctx: dict, summary_headings: list[str]) -> str:
    """Synthèse EFFECTIVE : éditée (étape 4) > harmonisée (relecture finale) > résumé LLM brut —
    la même priorité que le rapport Word. Jamais ``summary/summary.md``.

    ``summary`` est déjà la seule section synthèse. ``summary_harmonized`` et ``summary_llm`` sont
    des markdown COMPLETS (titre, type, participants, termes douteux… — mesuré : 16 Ko en médiane
    pour l'harmonisée) : on n'en garde que la section synthèse, comme le fait la relecture finale.
    """
    edited = str(meeting_ctx.get("summary") or "").strip()
    if edited:
        return edited
    for key in ("summary_harmonized", "summary_llm"):
        raw = str(meeting_ctx.get(key) or "")
        if raw.strip():
            return synthese_section(raw, summary_headings)
    return ""


def build_abstract(text: str, limit: int = ABSTRACT_MAX_CHARS) -> str:
    """Abrégé borné : blancs resserrés, coupe en fin de phrase (sinon de mot), « … » si tronqué."""
    compact = re.sub(r"[ \t]+", " ", str(text or ""))
    compact = re.sub(r"\s*\n\s*", "\n", compact).strip()
    if len(compact) <= limit:
        return compact
    head = compact[: limit - 1]
    floor = int(limit * 0.6)
    cut = max(head.rfind(". "), head.rfind(".\n"), head.rfind("\n"))
    if cut >= floor:
        return head[: cut + 1].rstrip() + "…"
    cut = head.rfind(" ")
    return (head[:cut] if cut >= floor else head).rstrip() + "…"


def _bounded_strings(value: object, *, max_items: int = MAX_LIST_ITEMS,
                     max_chars: int = MAX_ITEM_CHARS) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value:
        if isinstance(raw, (dict, list)) or raw is None:
            continue
        text = re.sub(r"\s+", " ", str(raw)).strip()
        if not text:
            continue
        items.append(text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…")
        if len(items) >= max_items:
            break
    return items


def _short_text(value: object, limit: int) -> str:
    """Champ court sur une ligne, borné (coupe au mot, « … » si tronqué). Non-texte ⇒ vide."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    cut = head.rfind(" ")
    return (head[:cut] if cut >= int(limit * 0.6) else head).rstrip() + "…"


def _validated_or_suggested(ctx: dict, validated_key: str, suggested_key: str) -> object:
    """Champ de l'étape 4 si elle a été enregistrée (même vidé : l'humain a tranché), sinon la
    suggestion de la LLM."""
    return ctx.get(validated_key) if type_is_validated(ctx) else ctx.get(suggested_key)


def _participants(value: object) -> list[dict]:
    """Personnes NOMMÉES de ``participants.json`` (nom, fonction) — une étiquette de diarisation
    (``SPEAKER_00``) n'est pas une personne et n'entre pas dans la fiche."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = re.sub(r"\s+", " ", str(entry.get("name") or "")).strip()[:120]
        if is_label(name) or fold(name) in seen:
            continue
        seen.add(fold(name))
        function = re.sub(r"\s+", " ", str(entry.get("function") or "")).strip()[:160]
        result.append({"name": name, "function": function})
        if len(result) >= MAX_PARTICIPANTS:
            break
    return result


def _speaker_name(entry: dict, mapping: dict) -> str:
    """Nom d'un locuteur : ``mapped_name`` des statistiques, sinon le mapping (dict ou chaîne nue —
    l'éditeur SRT écrit la forme courte), sinon rien (locuteur non nommé)."""
    name = str(entry.get("mapped_name") or "").strip()
    if not name:
        raw = mapping.get(str(entry.get("speaker_id") or ""))
        name = str((raw.get("name") if isinstance(raw, dict) else raw) or "").strip()
    return "" if is_label(name) else name[:120]


def speaking_shares(speaker_stats: object, speaker_mapping: object) -> tuple[list[dict], int]:
    """Parts du temps de parole par personne nommée (en %, décroissant, mêmes noms fusionnés) et
    part restante des locuteurs non nommés. Rien de mesurable ⇒ ``([], 0)``."""
    stats = speaker_stats if isinstance(speaker_stats, dict) else {}
    mapping_doc = speaker_mapping if isinstance(speaker_mapping, dict) else {}
    raw_mapping = mapping_doc.get("mapping")
    mapping: dict = raw_mapping if isinstance(raw_mapping, dict) else {}
    per_name: dict[str, float] = {}
    unnamed = 0.0
    total = 0.0
    for entry in stats.get("speakers") or []:
        if not isinstance(entry, dict):
            continue
        try:
            seconds = float(entry.get("speaking_time_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if seconds <= 0:
            continue
        total += seconds
        name = _speaker_name(entry, mapping)
        if name:
            per_name[name] = per_name.get(name, 0.0) + seconds
        else:
            unnamed += seconds
    if total <= 0:
        return [], 0
    shares = sorted(per_name.items(), key=lambda kv: -kv[1])[:MAX_PARTICIPANTS]
    return ([{"name": name, "pct": int(round(100 * seconds / total))} for name, seconds in shares],
            int(round(100 * unnamed / total)))


def project_card(meeting_ctx: object, participants: object, *, created_at: datetime | None,
                 confidential_types: frozenset[str], known_types: frozenset[str],
                 summary_headings: list[str], speaker_stats: object = None,
                 speaker_mapping: object = None) -> CardData:
    """Fiche d'une réunion depuis son contexte et ses participants.

    Args:
        meeting_ctx: contenu de ``context/meeting_context.json`` (ou ``None``/forme inattendue).
        participants: contenu de ``context/participants.json``.
        speaker_stats: contenu de ``speakers/speaker_stats.json`` (temps de parole par locuteur).
        speaker_mapping: contenu de ``speakers/speaker_mapping.json`` (locuteur → personne).
        created_at: date de création du job — repli de date, affiché « date d'import ».
        confidential_types: noms des types intégrés confidentiels.
        known_types: noms de tous les types intégrés.
        summary_headings: marqueurs de la section synthèse (langue du job puis toutes les autres).
    """
    ctx = meeting_ctx if isinstance(meeting_ctx, dict) else {}
    structured = ctx.get("structured_data")
    structured = structured if isinstance(structured, dict) else {}

    speaking = speaking_shares(speaker_stats, speaker_mapping)
    meeting_date = parse_meeting_date(ctx.get("date"))
    fallback = meeting_date is None
    if fallback and created_at is not None:
        meeting_date = created_at.date()

    return CardData(
        title=_short_text(ctx.get("title") or ctx.get("title_suggere"), _TITLE_MAX_CHARS),
        meeting_type=_short_text(_validated_or_suggested(ctx, "meeting_type", "type_suggere"), _TITLE_MAX_CHARS),
        meeting_date=meeting_date,
        date_is_import_fallback=fallback,
        confidential=is_confidential(ctx, confidential_types=confidential_types, known_types=known_types),
        sensitivity=str(ctx.get("sensitivity") or "normal").strip()[:20] or "normal",
        language=str(ctx.get("language") or "").strip()[:8],
        participants=_participants(participants),
        speaking=speaking[0],
        unnamed_speech_pct=speaking[1],
        topic=_short_text(_validated_or_suggested(ctx, "topic", "sujet_suggere"), TOPIC_MAX_CHARS),
        objective=_short_text(_validated_or_suggested(ctx, "objective", "objectif_suggere"), OBJECTIVE_MAX_CHARS),
        agenda=_short_text(_validated_or_suggested(ctx, "notes", "notes_suggeres"), AGENDA_MAX_CHARS),
        abstract=build_abstract(effective_synthesis(ctx, summary_headings)),
        decisions=_bounded_strings(structured.get("decisions")),
        actions=_bounded_strings(structured.get("actions")),
        next_date=_short_text(structured.get("prochaine_date"), _NEXT_DATE_MAX_CHARS),
    )


def source_fingerprint(meeting_raw: bytes | None, participants_raw: bytes | None, *,
                       created_at: datetime | None, confidential_types: frozenset[str],
                       known_types: frozenset[str], speaker_stats_raw: bytes | None = None,
                       speaker_mapping_raw: bytes | None = None) -> str:
    """Empreinte de TOUT ce dont la fiche dépend : les quatre fichiers (octets), la date de création,
    le catalogue des types et la version de la projection. Même empreinte ⇒ même fiche ⇒ rien à écrire."""
    digest = hashlib.sha256()
    for part in (
        f"v{CARD_VERSION}".encode(),
        meeting_raw if meeting_raw is not None else b"\x00absent",
        participants_raw if participants_raw is not None else b"\x00absent",
        speaker_stats_raw if speaker_stats_raw is not None else b"\x00absent",
        speaker_mapping_raw if speaker_mapping_raw is not None else b"\x00absent",
        (created_at.isoformat() if created_at else "").encode(),
        "\n".join(sorted(confidential_types)).encode(),
        "\n".join(sorted(known_types)).encode(),
    ):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()

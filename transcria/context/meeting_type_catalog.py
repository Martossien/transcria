"""Catalogue des types de réunion — source unique en données.

SOURCE UNIQUE = ``transcria/data/meeting_types.yaml`` (versionné). Plus aucun type,
champ de saisie, thème DOCX ou drapeau de comportement n'est écrit en dur dans le
code : les anciens ``MEETING_TYPES`` / ``TYPE_SPECIFIC_FIELDS`` (meeting_context) et
``_THEMES`` / ``_CSE_TYPES`` / ``_AUTO_CONFIDENTIEL`` (docx_report) sont dérivés d'ici.
Cf. docs/TYPES_REUNION_PERSONNALISES.md (lot A) — le lot B branchera les types
personnalisés (base de données) sur le même schéma via ``validate_type_definition``.

Le fichier intégré est un contrat : toute entrée invalide fait échouer le chargement
IMMÉDIATEMENT (import du module consommateur), jamais silencieusement — c'est le même
principe fail-loud que ``config/llm_profiles.py``.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

import yaml

_BUILTIN_PATH = Path(__file__).resolve().parent.parent / "data" / "meeting_types.yaml"

SCHEMA_VERSION = 1

# Bornes du schéma — partagées avec la validation des types personnalisés (lot B)
# et le format d'échange communautaire (mêmes limites à l'import).
MAX_NAME_LEN = 80
MAX_BADGE_LEN = 16
MAX_BANNER_LEN = 80
MAX_HINT_LEN = 200
MAX_FIELDS = 12
MAX_HINTS = 8

FIELD_TYPES = frozenset({"text", "number", "textarea"})
# Unités du registre de sections DOCX (source de vérité — docx_report s'aligne dessus).
ORDERABLE_SECTIONS = ("contexte", "synthese", "champs_type", "pv", "participants", "transcript", "quality")
TOGGLEABLE_SECTIONS = ("participants", "transcript", "quality")
MAX_FOOTER_LEN = 120
# Clés UNIVERSELLES du JSON § 9 du prompt de résumé — les extract_fields d'un type
# ne peuvent pas les masquer.
UNIVERSAL_EXTRACT_KEYS = frozenset({
    "decisions", "actions", "blocages", "reports", "votes",
    "resolutions", "points_odj", "prochaine_date",
})
MAX_EXTRACT_FIELDS = 6
MAX_EXTRACT_INSTRUCTION_LEN = 200
# Caractères interdits dans une instruction d'extraction (réduction de la surface
# d'injection de prompt : pas de code, pas de structure JSON, pas de guillemets doubles).
_EXTRACT_FORBIDDEN = ("`", '"', "{", "}")
_HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}$")
_PALETTE_KEYS = ("primary", "accent", "light")
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MeetingTypeCatalogError(ValueError):
    """Définition de type invalide (fichier intégré corrompu ou saisie rejetée)."""


def _fail(name: str, message: str) -> NoReturn:
    raise MeetingTypeCatalogError(f"type de réunion {name!r} : {message}")


def _validated_str(name: str, value: Any, label: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(name, f"{label} doit être une chaîne non vide")
    text = str(value).strip()
    if len(text) > max_len:
        _fail(name, f"{label} dépasse {max_len} caractères")
    if "\n" in text:
        _fail(name, f"{label} doit tenir sur une ligne")
    return text


def validate_type_definition(raw: Any) -> dict:
    """Valide et NORMALISE une définition de type (intégrée, personnalisée ou importée).

    Retourne un dict complet : ``{name, badge, banner_text, palette (dict|None),
    behavior {quorum, confidential}, fields (list), detection_hints (list)}``.
    Tout écart au schéma lève :class:`MeetingTypeCatalogError` avec un message
    actionnable — jamais de nettoyage silencieux (contrat §8.2 du cadrage).
    """
    if not isinstance(raw, dict):
        raise MeetingTypeCatalogError("une définition de type doit être un objet")
    unknown = set(raw) - {"name", "badge", "banner_text", "palette", "behavior", "fields",
                          "detection_hints", "sections", "branding", "extract_fields"}
    if unknown:
        raise MeetingTypeCatalogError(f"clés inconnues : {sorted(unknown)}")

    name = _validated_str("?", raw.get("name"), "name", MAX_NAME_LEN)

    badge = ""
    if raw.get("badge") is not None:
        badge = _validated_str(name, raw["badge"], "badge", MAX_BADGE_LEN)
    banner_text = ""
    if raw.get("banner_text") is not None:
        banner_text = _validated_str(name, raw["banner_text"], "banner_text", MAX_BANNER_LEN)

    palette: dict[str, str] | None = None
    if raw.get("palette") is not None:
        raw_palette = raw["palette"]
        if not isinstance(raw_palette, dict) or set(raw_palette) != set(_PALETTE_KEYS):
            _fail(name, f"palette doit contenir exactement {list(_PALETTE_KEYS)}")
        palette = {}
        for key in _PALETTE_KEYS:
            color = raw_palette[key]
            if not isinstance(color, str) or not _HEX_COLOR.match(color):
                _fail(name, f"palette.{key} doit être un hex à 6 chiffres sans '#' (reçu {color!r})")
            palette[key] = color.upper()
        if not banner_text:
            _fail(name, "banner_text est requis quand une palette est définie")

    behavior_raw = raw.get("behavior") or {}
    if not isinstance(behavior_raw, dict) or set(behavior_raw) - {"quorum", "confidential"}:
        _fail(name, "behavior n'accepte que les booléens 'quorum' et 'confidential'")
    behavior = {
        "quorum": bool(behavior_raw.get("quorum", False)),
        "confidential": bool(behavior_raw.get("confidential", False)),
    }

    fields: list[dict[str, str]] = []
    raw_fields = raw.get("fields") or []
    if not isinstance(raw_fields, list) or len(raw_fields) > MAX_FIELDS:
        _fail(name, f"fields doit être une liste de {MAX_FIELDS} entrées maximum")
    seen_keys: set[str] = set()
    for entry in raw_fields:
        if not isinstance(entry, dict) or not {"key", "label", "type"} <= set(entry) \
                or set(entry) - {"key", "label", "type", "short_label"}:
            _fail(name, "chaque champ doit définir key/label/type (+ short_label optionnel)")
        key = entry["key"]
        if not isinstance(key, str) or not _FIELD_KEY.match(key):
            _fail(name, f"clé de champ invalide : {key!r} (minuscules/chiffres/underscore)")
        if key in seen_keys:
            _fail(name, f"clé de champ dupliquée : {key!r}")
        seen_keys.add(key)
        if entry["type"] not in FIELD_TYPES:
            _fail(name, f"type de champ inconnu : {entry['type']!r} (attendu {sorted(FIELD_TYPES)})")
        label = _validated_str(name, entry["label"], f"label du champ {key!r}", MAX_NAME_LEN)
        field = {"key": key, "label": label, "type": entry["type"]}
        if entry.get("short_label") is not None:
            # Libellé court pour le tableau du rapport DOCX (défaut : le label).
            field["short_label"] = _validated_str(name, entry["short_label"], f"short_label du champ {key!r}", MAX_NAME_LEN)
        fields.append(field)

    hints: list[str] = []
    raw_hints = raw.get("detection_hints") or []
    if not isinstance(raw_hints, list) or len(raw_hints) > MAX_HINTS:
        _fail(name, f"detection_hints doit être une liste de {MAX_HINTS} entrées maximum")
    for hint in raw_hints:
        hints.append(_validated_str(name, hint, "indice de détection", MAX_HINT_LEN))

    sections_raw = raw.get("sections") or {}
    if not isinstance(sections_raw, dict) or set(sections_raw) - {"order", "enabled"}:
        _fail(name, "sections n'accepte que 'order' et 'enabled'")
    sections: dict = {}
    raw_order = sections_raw.get("order")
    if raw_order is not None:
        if not isinstance(raw_order, list) or not set(raw_order) <= set(ORDERABLE_SECTIONS) \
                or len(raw_order) != len(set(raw_order)):
            _fail(name, f"sections.order : liste sans doublon parmi {list(ORDERABLE_SECTIONS)}")
        sections["order"] = list(raw_order)
    raw_enabled = sections_raw.get("enabled")
    if raw_enabled is not None:
        if not isinstance(raw_enabled, dict) or set(raw_enabled) - set(TOGGLEABLE_SECTIONS) \
                or not all(isinstance(v, bool) for v in raw_enabled.values()):
            _fail(name, f"sections.enabled : booléens parmi {list(TOGGLEABLE_SECTIONS)}")
        sections["enabled"] = dict(raw_enabled)

    extract_fields: list[dict[str, str]] = []
    raw_extract = raw.get("extract_fields") or []
    if not isinstance(raw_extract, list) or len(raw_extract) > MAX_EXTRACT_FIELDS:
        _fail(name, f"extract_fields doit être une liste de {MAX_EXTRACT_FIELDS} entrées maximum")
    seen_extract: set[str] = set()
    for entry in raw_extract:
        if not isinstance(entry, dict) or set(entry) != {"key", "label", "instruction"}:
            _fail(name, "chaque champ d'extraction doit définir exactement key/label/instruction")
        key = entry["key"]
        if not isinstance(key, str) or not _FIELD_KEY.match(key):
            _fail(name, f"clé d'extraction invalide : {key!r} (minuscules/chiffres/underscore)")
        if key in UNIVERSAL_EXTRACT_KEYS:
            _fail(name, f"clé d'extraction réservée : {key!r} (champ universel du résumé)")
        if key in seen_extract:
            _fail(name, f"clé d'extraction dupliquée : {key!r}")
        seen_extract.add(key)
        label = _validated_str(name, entry["label"], f"label d'extraction {key!r}", MAX_NAME_LEN)
        instruction = _validated_str(name, entry["instruction"], f"instruction d'extraction {key!r}",
                                     MAX_EXTRACT_INSTRUCTION_LEN)
        for forbidden in _EXTRACT_FORBIDDEN:
            if forbidden in instruction:
                _fail(name, f"instruction d'extraction {key!r} : caractère interdit {forbidden!r} "
                            "(texte descriptif uniquement)")
        extract_fields.append({"key": key, "label": label, "instruction": instruction})

    branding_raw = raw.get("branding") or {}
    if not isinstance(branding_raw, dict) or set(branding_raw) - {"footer_text"}:
        _fail(name, "branding n'accepte que 'footer_text' (le logo est un binaire à part)")
    branding: dict = {}
    if branding_raw.get("footer_text") is not None:
        branding["footer_text"] = _validated_str(name, branding_raw["footer_text"], "footer_text", MAX_FOOTER_LEN)

    return {
        "name": name,
        "badge": badge,
        "banner_text": banner_text,
        "palette": palette,
        "behavior": behavior,
        "fields": fields,
        "detection_hints": hints,
        "sections": sections,
        "branding": branding,
        "extract_fields": extract_fields,
    }


@lru_cache(maxsize=1)
def load_builtin_types() -> tuple[dict, ...]:
    """Charge et valide le catalogue intégré (fail-loud, mis en cache).

    L'ordre du fichier est préservé : c'est l'ordre du menu de l'étape 4.
    """
    try:
        raw = yaml.safe_load(_BUILTIN_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MeetingTypeCatalogError(f"catalogue intégré illisible ({_BUILTIN_PATH}): {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise MeetingTypeCatalogError(f"schema_version {SCHEMA_VERSION} attendu dans {_BUILTIN_PATH}")
    entries = raw.get("types")
    if not isinstance(entries, list) or not entries:
        raise MeetingTypeCatalogError("le catalogue intégré doit définir une liste 'types' non vide")
    validated = [validate_type_definition(entry) for entry in entries]
    names = [t["name"] for t in validated]
    if len(names) != len(set(names)):
        raise MeetingTypeCatalogError("noms de types dupliqués dans le catalogue intégré")
    return tuple(validated)


# ── Accesseurs (vues dérivées, formes attendues par les consommateurs) ──────────

def meeting_type_names() -> list[str]:
    """Noms des types intégrés, dans l'ordre du menu (ex-``MEETING_TYPES``)."""
    return [t["name"] for t in load_builtin_types()]


def type_specific_fields() -> dict[str, list[dict]]:
    """Champs de saisie par type — seuls les types qui en ont (ex-``TYPE_SPECIFIC_FIELDS``)."""
    return {t["name"]: list(t["fields"]) for t in load_builtin_types() if t["fields"]}


def theme_specs() -> dict[str, dict]:
    """Spécifications de thème par type À PALETTE (ex-``_THEMES``, couleurs en hex).

    ``{name: {palette: {primary, accent, light}, banner_text, badge}}`` — les types
    sans palette (Réunion interne, Autre) utilisent le thème par défaut du renderer.
    """
    return {
        t["name"]: {"palette": dict(t["palette"]), "banner_text": t["banner_text"], "badge": t["badge"]}
        for t in load_builtin_types()
        if t["palette"] is not None
    }


def field_short_labels() -> dict[str, str]:
    """Libellé COURT d'affichage DOCX par clé de champ, tous types confondus
    (ex-``LABELS`` de ``_section_type_specific``) — ``short_label`` sinon ``label``."""
    labels: dict[str, str] = {}
    for t in load_builtin_types():
        for field in t["fields"]:
            labels[field["key"]] = field.get("short_label") or field["label"]
    return labels


def quorum_types() -> frozenset[str]:
    """Types à quorum + objet de séance sur la page de garde (ex-``_CSE_TYPES``)."""
    return frozenset(t["name"] for t in load_builtin_types() if t["behavior"]["quorum"])


def confidential_types() -> frozenset[str]:
    """Types au badge confidentiel forcé (ex-``_AUTO_CONFIDENTIEL``)."""
    return frozenset(t["name"] for t in load_builtin_types() if t["behavior"]["confidential"])


def detection_hints() -> dict[str, list[str]]:
    """Indices de sélection du « Type suggéré » par type (consommés au lot D —
    le prompt de résumé garde sa copie en dur d'ici là, synchrone par test)."""
    return {t["name"]: list(t["detection_hints"]) for t in load_builtin_types() if t["detection_hints"]}

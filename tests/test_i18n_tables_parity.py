"""Parité des tables i18n « par langue » hors gettext (revue PR #15, 2026-10-22).

Chaque table ``{lang: {clé: texte}}`` doit offrir les MÊMES clés dans chaque langue, et
les textes à placeholders doivent se formater avec les mêmes variables que le français :
une clé orpheline (« Archive ZIP complet ») ou un placeholder renommé (« {editado(s)} »)
rendait une traduction morte, voire un KeyError à l'exécution.
"""
from __future__ import annotations

import re

import pytest

_PLACEHOLDER = re.compile(r"\{(\w+)[^}]*\}")


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def _tables():
    from transcria.exports.docx_style import _DOCX_LABELS
    from transcria.quality.light_report import _STRINGS as light
    from transcria.quality.quality_report import _QR_STRINGS
    from transcria.quality.review_points import _RP_STRINGS
    from transcria.web.editor_routes import _SYNC_SUMMARY_STRINGS
    from transcria.workflow.phases.correction import _MSG
    from transcria.workflow.phases.refine import _REFINE_MESSAGES
    from transcria.workflow.progress import _PROGRESS_MESSAGES
    from transcria.workflow.speaker_projection import _SP_STRINGS
    return {
        "docx_style": _DOCX_LABELS, "light_report": light, "quality_report": _QR_STRINGS,
        "review_points": _RP_STRINGS, "editor_sync": _SYNC_SUMMARY_STRINGS, "correction": _MSG,
        "refine": _REFINE_MESSAGES, "progress": _PROGRESS_MESSAGES, "speaker_projection": _SP_STRINGS,
    }


@pytest.mark.parametrize("name", list(_tables()))
def test_chaque_langue_a_les_cles_du_francais(name):
    table = _tables()[name]
    ref = table["fr"]
    for lang, strings in table.items():
        assert set(strings) == set(ref), f"{name}[{lang}] : clés ≠ fr ({set(strings) ^ set(ref)})"


@pytest.mark.parametrize("name", list(_tables()))
def test_memes_placeholders_que_le_francais(name):
    table = _tables()[name]
    ref = table["fr"]
    for lang, strings in table.items():
        for key, text in strings.items():
            assert _placeholders(text) == _placeholders(ref[key]), f"{name}[{lang}][{key}] : placeholders ≠ fr"


def test_profils_pt_sans_cle_orpheline():
    from transcria.workflow.profiles_i18n import _TABLES
    ref = set(_TABLES["en"])
    for lang, table in _TABLES.items():
        assert set(table) == ref, f"profiles_i18n[{lang}] : clés ≠ en ({set(table) ^ ref})"


def test_sentinelle_participant_de_chaque_prompt_reconnue():
    from transcria.llm_tools.llm_parsing import is_non_identifiable_participant_line
    for sentinel in ("(non identifiable)", "(not identifiable)", "(nicht identifizierbar)",
                     "(no identificable)", "(non identificabile)", "(não identificável)"):
        assert is_non_identifiable_participant_line(f"- {sentinel}"), sentinel


def test_marqueur_participant_count_present_dans_chaque_prompt(tmp_path):
    from pathlib import Path

    from transcria.llm_tools.prompt_locator import _SUMMARY_MARKERS
    root = Path(__file__).resolve().parents[1] / "configs" / "prompts"
    for lang, markers in _SUMMARY_MARKERS.items():
        path = root / "summary_prompt.txt" if lang == "fr" else root / lang / "summary_prompt.txt"
        if not path.is_file():
            continue
        assert markers["participant_count"] in path.read_text(encoding="utf-8"), lang


def test_alignement_force_saute_une_langue_sans_modele(monkeypatch):
    from transcria.stt.forced_alignment import ForcedAlignmentService
    svc = ForcedAlignmentService({"whisper": {"forced_alignment": {"enabled": True}}})
    monkeypatch.setattr(ForcedAlignmentService, "available", property(lambda self: True))
    segs = [{"start": 0.0, "end": 1.0, "text": "olá"}]
    assert svc.align_segments(None, segs, language="pt") is segs

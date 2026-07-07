"""Axe B (Vague 4) : langue des livrables — résolution de langue, prompt localisé, consigne.

Couvre le PLUMBING pur (GPU-free). La qualité de rédaction EN et le parsing bout-en-bout
relèvent d'une passe E2E GPU (bêta), hors périmètre de ces tests unitaires.
"""
from __future__ import annotations

from pathlib import Path

from transcria.gpu.opencode_runner import (
    language_directive,
    resolve_output_language,
    resolve_prompt_file,
)


def test_resolve_output_language_defaults_to_fr():
    assert resolve_output_language(extra_data={}) == "fr"
    assert resolve_output_language(extra_data={"meeting_context": {}}) == "fr"


def test_resolve_output_language_reads_meeting_context():
    assert resolve_output_language(extra_data={"meeting_context": {"language": "en"}}) == "en"
    assert resolve_output_language(extra_data={"meeting_context": {"language": "de"}}) == "de"


def test_resolve_output_language_from_job_object():
    class _Job:
        def get_extra_data(self):
            return {"meeting_context": {"language": "es"}}
    assert resolve_output_language(job=_Job()) == "es"


def test_resolve_prompt_file_fr_uses_root(tmp_path: Path):
    base = tmp_path / "prompts"
    base.mkdir()
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    assert resolve_prompt_file(cfg, "summary_prompt.txt", "fr") == str((base / "summary_prompt.txt").resolve())


def test_resolve_prompt_file_en_prefers_localized(tmp_path: Path):
    base = tmp_path / "prompts"
    (base / "en").mkdir(parents=True)
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    (base / "en" / "summary_prompt.txt").write_text("EN", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    resolved = resolve_prompt_file(cfg, "summary_prompt.txt", "en")
    assert resolved == str((base / "en" / "summary_prompt.txt").resolve())
    assert Path(resolved).read_text(encoding="utf-8") == "EN"


def test_resolve_prompt_file_en_falls_back_to_fr_when_missing(tmp_path: Path):
    base = tmp_path / "prompts"
    base.mkdir()
    (base / "summary_prompt.txt").write_text("FR", encoding="utf-8")
    cfg = {"workflow": {"prompts_dir": str(base)}}
    # pas de en/ → repli sur la racine (français source) : non-régression garantie
    assert resolve_prompt_file(cfg, "summary_prompt.txt", "en") == str((base / "summary_prompt.txt").resolve())


def test_language_directive_empty_for_fr():
    assert language_directive("fr") == ""
    assert language_directive("") == ""


def test_language_directive_mentions_target():
    d = language_directive("en")
    assert "English" in d and "marqueurs de format" in d


# ── Parsers pilotés par la langue (table de marqueurs) ────────────────────────

_FR_SUMMARY = """## Synthèse
Texte de synthèse d'exemple (placeholder, aucun extrait réel).

**Titre suggéré :** Titre Alpha
**Type suggéré :** CSE
**Sujet principal :** Sujet Beta
**Objectif probable :** Objectif Gamma
**Notes / Ordre du jour probable :** Note Delta
**Mots-clés**
mot1, mot2
**Nombre de participants détectés :** 2

## Participants probables
- SPEAKER_00 [Alpha] : présidence
- SPEAKER_01 : Beta — rapporteur

## Termes suspects/douteux
| terme | variantes | catégorie | priorité |
| Epsilon | epslon | mot suspect | normale |

## Données structurées
```json
{"decisions": ["D1"], "actions": ["A1"]}
```
"""


def _to_english(md: str) -> str:
    from transcria.gpu.opencode_runner import summary_markers
    fr, en = summary_markers("fr"), summary_markers("en")
    for key in ("title", "type", "subject", "objective", "notes", "keywords",
                "participant_count", "participants_heading", "summary_heading"):
        md = md.replace(fr[key], en[key])
    md = md.replace("## Termes suspects/douteux", "## Doubtful terms")
    md = md.replace("## Données structurées", "## Structured data")
    return md


def test_summary_parser_fr_unchanged():
    """Non-régression : le résumé français parse comme avant (défaut fr)."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    p = OpenCodeRunner._parse_structured_summary(_FR_SUMMARY)  # défaut language="fr"
    assert p["title_suggere"] == "Titre Alpha"
    assert p["type_suggere"] == "CSE"
    assert p["sujet_suggere"] == "Sujet Beta"
    assert p["objectif_suggere"] == "Objectif Gamma"
    assert p["speaker_count"] == 2
    assert "SPEAKER_00" in p["participants_detectes"] and "SPEAKER_01" in p["participants_detectes"]
    assert [t["term"] for t in p["termes_suspects"]] == ["Epsilon"]
    assert p["structured_data"]["decisions"] == ["D1"]
    assert p["structured_data"]["actions"] == ["A1"]


def test_summary_parser_en_markers_extract_same_fields():
    """Le même contenu avec marqueurs anglais parse identiquement en mode en."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    en_md = _to_english(_FR_SUMMARY)
    p = OpenCodeRunner._parse_structured_summary(en_md, (), "en")
    assert p["title_suggere"] == "Titre Alpha"      # les VALEURS ne changent pas dans ce test
    assert p["speaker_count"] == 2
    assert "SPEAKER_00" in p["participants_detectes"]
    assert [t["term"] for t in p["termes_suspects"]] == ["Epsilon"]
    assert p["structured_data"]["decisions"] == ["D1"]


def test_en_markers_not_found_in_fr_mode():
    """Garde-fou : les marqueurs EN ne sont PAS lus en mode fr (isolation des chemins)."""
    from transcria.gpu.opencode_runner import OpenCodeRunner
    en_md = _to_english(_FR_SUMMARY)
    p_fr = OpenCodeRunner._parse_structured_summary(en_md, (), "fr")  # marqueurs EN, parser FR
    assert p_fr["title_suggere"] == ""              # le parser FR ne trouve pas "Suggested title"
    assert p_fr["speaker_count"] == 0


def test_effective_summary_marker_by_language():
    """La section synthèse remplacée dépend de la langue du meeting."""
    from transcria.context.meeting_context import MeetingContextManager
    raw_fr = "## Synthèse\nancien texte\n## Autre\nx"
    out = MeetingContextManager.effective_summary_markdown({"summary": "NOUVEAU", "language": "fr"}, raw_fr)
    assert "NOUVEAU" in out and "## Synthèse" in out
    raw_en = "## Summary\nold text\n## Other\nx"
    out_en = MeetingContextManager.effective_summary_markdown({"summary": "NEW", "language": "en"}, raw_en)
    assert "NEW" in out_en and "## Summary" in out_en

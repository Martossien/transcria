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


def test_language_directive_mentions_target_and_preserves_markers():
    d = language_directive("en")
    assert "English" in d
    # Doit protéger les marqueurs de format (parser-safe)
    assert "marqueurs de format" in d and "ne les traduis pas" in d

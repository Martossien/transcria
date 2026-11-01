"""Mention « LLM externe » au pied du DOCX (docs/LLM_CLOUD.md § 3.2.6)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("docx")


def _seed(tmp: Path, job_id: str) -> None:
    path = Path(__file__).parent / "test_docx_report.py"
    spec = importlib.util.spec_from_file_location("_docx_seed_module_prov", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._seed_job(tmp, job_id)


def _footer_text(tmp_path: Path, provenance: str | dict | None) -> str:
    from docx import Document

    from transcria.exports.docx_report import generate_docx_report

    jobs = tmp_path / "jobs"
    job_id = "job-prov-001"
    _seed(jobs, job_id)
    if provenance is not None:
        raw = provenance if isinstance(provenance, str) else json.dumps(provenance)
        (jobs / job_id / "metadata" / "llm_provenance.json").write_text(raw, encoding="utf-8")
    out = tmp_path / "r.docx"
    generate_docx_report(job_id, str(jobs), out)
    doc = Document(str(out))
    return "\n".join(p.text for p in doc.sections[0].footer.paragraphs)


def test_no_provenance_file_has_no_mention(tmp_path):
    assert "LLM externe" not in _footer_text(tmp_path, None)


def test_external_provenance_adds_mention_before_transcria(tmp_path):
    text = _footer_text(tmp_path, {"external": True, "provider": "mistral", "provider_label": "Fournisseur A"})
    assert "LLM externe : Fournisseur A" in text
    assert text.index("LLM externe") < text.index("TranscrIA")


def test_provider_used_when_no_label(tmp_path):
    text = _footer_text(tmp_path, {"external": True, "provider": "mistral"})
    assert "LLM externe : mistral" in text


def test_not_external_has_no_mention(tmp_path):
    assert "LLM externe" not in _footer_text(tmp_path, {"external": False, "provider": "mistral"})


def test_corrupt_provenance_does_not_break_rendering(tmp_path):
    text = _footer_text(tmp_path, "{pas du json")
    assert "LLM externe" not in text
    assert "TranscrIA" in text

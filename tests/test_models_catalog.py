"""Catalogue des modèles : résolution config-driven, gated, statut présent/absent, disque."""
from __future__ import annotations

from pathlib import Path

import transcria.models_catalog as mc
from transcria.models_catalog import (
    ModelSpec,
    build_catalog,
    catalog_with_status,
    disk_free_bytes,
    model_status,
)


def _cfg(stt="cohere", diar="pyannote") -> dict:
    return {"models": {"stt_backend": stt, "diarization_backend": diar}}


def test_build_catalog_stt_and_diar_without_vram():
    specs = build_catalog(_cfg())
    roles = {s.role for s in specs}
    assert roles == {"stt", "diarization"}  # pas de LLM sans VRAM fourni


def test_build_catalog_includes_llm_when_vram_known():
    specs = build_catalog(_cfg(), total_vram_mb=64000)
    llm = [s for s in specs if s.role == "arbitrage_llm"]
    assert llm and llm[0].kind == "gguf" and llm[0].file and llm[0].gated is False


def test_gated_flags_cohere_and_pyannote():
    specs = {s.role: s for s in build_catalog(_cfg("cohere", "pyannote"))}
    assert specs["stt"].gated is True          # Cohere = accès repo
    assert specs["diarization"].gated is True   # pyannote = token + licence


def test_non_gated_whisper_and_sortformer():
    specs = {s.role: s for s in build_catalog(_cfg("whisper", "sortformer"))}
    assert specs["stt"].gated is False and "MIT" in specs["stt"].license
    assert specs["diarization"].gated is False


def test_model_status_gguf_present(tmp_path: Path):
    spec = ModelSpec("arbitrage_llm", "L", "repo/x", "m.gguf", "gguf", "sub", False, "MIT", "u", 20.0)
    gguf = tmp_path / "sub" / "m.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"x" * 2048)
    status = model_status(spec, hf_home=tmp_path / "hf", models_dir=tmp_path)
    assert status["present"] is True and status["size_bytes"] == 2048


def test_model_status_gguf_absent(tmp_path: Path):
    spec = ModelSpec("arbitrage_llm", "L", "repo/x", "m.gguf", "gguf", "sub", False, "MIT", "u", 20.0)
    assert model_status(spec, hf_home=tmp_path, models_dir=tmp_path)["present"] is False


def test_model_status_hf_cache_present(tmp_path: Path, monkeypatch):
    cached = tmp_path / "cache" / "models--x"
    (cached).mkdir(parents=True)
    (cached / "f.bin").write_bytes(b"y" * 4096)
    monkeypatch.setattr(mc, "find_hf_cache_model", lambda hf, repo: cached)
    spec = ModelSpec("stt", "S", "x/y", None, "hf_cache", "", False, "MIT", "u", 1.0)
    status = model_status(spec, hf_home=tmp_path, models_dir=tmp_path)
    assert status["present"] is True and status["size_bytes"] == 4096


def test_disk_free_bytes_positive(tmp_path: Path):
    assert disk_free_bytes(tmp_path / "does" / "not" / "exist") > 0  # remonte au parent existant


def test_catalog_with_status_shape(monkeypatch):
    monkeypatch.setattr(mc, "find_hf_cache_model", lambda hf, repo: None)
    view = catalog_with_status(_cfg(), total_vram_mb=24000)
    assert set(view) >= {"items", "hf_home", "models_dir", "hf_free_gb", "models_free_gb"}
    assert all("present" in it and "spec" in it for it in view["items"])
    assert any(it["spec"].role == "arbitrage_llm" for it in view["items"])

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


def test_served_llm_gguf_parses_launch_script(tmp_path: Path):
    from transcria.models_catalog import served_llm_gguf

    script = tmp_path / "launch.sh"
    script.write_text("llama-server \\\n--model /root/models/x/Model-Q8.gguf \\\n--port 8080\n")
    assert served_llm_gguf({"services": {"arbitrage_script": str(script)}}) == Path("/root/models/x/Model-Q8.gguf")


def test_served_llm_gguf_expands_models_dir_template(tmp_path: Path, monkeypatch):
    from transcria.models_catalog import served_llm_gguf

    monkeypatch.setenv("MODELS_DIR", "/data/models")
    script = tmp_path / "launch.sh"
    script.write_text('--model "${MODELS_DIR:-/home/x/models}/Sub/Model.gguf" \\\n')
    assert served_llm_gguf({"services": {"arbitrage_script": str(script)}}) == Path("/data/models/Sub/Model.gguf")


def test_model_status_finds_served_llm_anywhere(tmp_path: Path):
    served = tmp_path / "served" / "Model-Q8.gguf"
    served.parent.mkdir(parents=True)
    served.write_bytes(b"x" * 100)
    spec = ModelSpec("arbitrage_llm", "L", "r/x", "Model-Q8.gguf", "gguf", "expected-subdir", False, "MIT", "u", 38.0)
    status = model_status(spec, hf_home=tmp_path / "hf", models_dir=tmp_path / "nope", served_path=served)
    assert status["present"] and status["path"] == str(served) and status["size_bytes"] == 100


def test_model_status_gguf_glob_finds_file_in_other_subdir(tmp_path: Path):
    actual = tmp_path / "weird-name" / "Model.gguf"   # sous-dossier ≠ target_subdir attendu
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"y" * 64)
    spec = ModelSpec("arbitrage_llm", "L", "r/x", "Model.gguf", "gguf", "expected", False, "MIT", "u", 1.0)
    status = model_status(spec, hf_home=tmp_path / "hf", models_dir=tmp_path)
    assert status["present"] and status["path"] == str(actual)


def test_model_status_hf_cache_probes_hub_subdir(tmp_path: Path, monkeypatch):
    # LE bug corrigé : les modèles sont dans <hf_home>/hub/models--… (pas <hf_home>/models--…)
    seen: dict = {}

    def fake_find(hub, repo):
        seen.setdefault("hubs", []).append(str(hub))
        return (hub / ("models--" + repo.replace("/", "--"))) if Path(hub).name == "hub" else None

    monkeypatch.setattr(mc, "find_hf_cache_model", fake_find)
    spec = ModelSpec("stt", "S", "Org/Model", None, "hf_cache", "", False, "MIT", "u", 1.0)
    status = model_status(spec, hf_home=tmp_path, models_dir=tmp_path)
    assert status["present"] is True
    assert any(h.endswith("/hub") or h.endswith("\\hub") for h in seen["hubs"])


def test_disk_free_bytes_positive(tmp_path: Path):
    assert disk_free_bytes(tmp_path / "does" / "not" / "exist") > 0  # remonte au parent existant


def test_catalog_with_status_shape(monkeypatch):
    monkeypatch.setattr(mc, "find_hf_cache_model", lambda hf, repo: None)
    view = catalog_with_status(_cfg(), total_vram_mb=24000)
    assert set(view) >= {"items", "hf_home", "models_dir", "hf_free_gb", "models_free_gb"}
    assert all("present" in it and "spec" in it for it in view["items"])
    assert any(it["spec"].role == "arbitrage_llm" for it in view["items"])

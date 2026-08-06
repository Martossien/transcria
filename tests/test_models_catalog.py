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


def test_find_hf_cache_model_unreadable_dir_returns_none(tmp_path: Path):
    """Cache HF non lisible (ex. /root/.cache en CI) → ABSENT, jamais de PermissionError
    qui casserait le rendu de /admin/models (régression CI)."""
    import os

    import pytest

    from transcria.installer.models_lib import find_hf_cache_model

    if os.geteuid() == 0:
        pytest.skip("root ignore les permissions de fichier")
    cache = tmp_path / "models--CohereLabs--cohere-transcribe-03-2026"
    (cache / "snapshots" / "abc").mkdir(parents=True)
    os.chmod(cache, 0o000)
    try:
        assert find_hf_cache_model(tmp_path, "CohereLabs/cohere-transcribe-03-2026") is None
    finally:
        os.chmod(cache, 0o755)  # pour que pytest puisse nettoyer tmp_path


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


class TestServedSttCatalog:
    """Lignes catalogue des moteurs STT servis (runtimes C++) : proposées quand le
    moteur est déclaré (manifeste) ou le backend routé ; kind runtime sondé sous
    runtimes/ ; kind gguf = machinerie existante."""

    def _cfg(self, *, engine=None, backend_url=None):
        cfg = {"models": {"stt_backend": "whisper", "diarization_backend": "sortformer"}}
        if engine:
            cfg["resource_node"] = {"engines": [{"name": engine, "script": "s.sh",
                                                 "gpu": 0, "port": 8021}]}
        if backend_url:
            cfg["inference"] = {"mode": "hybrid",
                                "stt": {"backends": {"qwen3asr": {"url": backend_url}}}}
        return cfg

    def test_moteur_declare_propose_la_ligne(self):
        from transcria.models_catalog import build_catalog

        specs = build_catalog(self._cfg(engine="qwen3asr"))
        served = [s for s in specs if s.role == "stt_served"]
        assert len(served) == 1
        assert served[0].repo_id == "Qwen/Qwen3-ASR-1.7B-hf"
        assert served[0].kind == "runtime"
        assert served[0].file == "qwen3_asr_1_7b_hf"  # id du paquet délégué

    def test_backend_route_sans_manifeste_propose_aussi(self):
        from transcria.models_catalog import build_catalog

        specs = build_catalog(self._cfg(backend_url="http://127.0.0.1:8021/v1"))
        assert any(s.role == "stt_served" and s.kind == "runtime" for s in specs)

    def test_nemotron_est_un_gguf_classique(self):
        from transcria.models_catalog import build_catalog

        cfg = self._cfg()
        cfg["resource_node"] = {"engines": [{"name": "nemotron", "script": "s.sh",
                                             "gpu": 0, "port": 8022}]}
        served = [s for s in build_catalog(cfg) if s.role == "stt_served"]
        assert served[0].kind == "gguf"
        assert served[0].file.endswith(".gguf")
        assert served[0].target_subdir == "parakeet-cpp"

    def test_aucun_moteur_aucune_ligne(self):
        from transcria.models_catalog import build_catalog

        assert not any(s.role == "stt_served" for s in build_catalog(self._cfg()))

    def test_statut_runtime_sonde_sous_runtimes(self, monkeypatch, tmp_path):
        from transcria.models_catalog import build_catalog, model_status

        monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
        spec = [s for s in build_catalog(self._cfg(engine="qwen3asr"))
                if s.role == "stt_served"][0]
        st = model_status(spec, hf_home=tmp_path / "hf", models_dir=tmp_path / "m")
        assert st["present"] is False
        target = tmp_path / "audiocpp" / "src" / "models" / "Qwen3-ASR-1.7B-hf"
        target.mkdir(parents=True)
        (target / "config.json").write_text("{}")
        st = model_status(spec, hf_home=tmp_path / "hf", models_dir=tmp_path / "m")
        assert st["present"] is True and st["size_bytes"] > 0


class TestOllamaBackendCatalog:
    """Backend Ollama : la page montre le modèle CONFIGURÉ, jamais un GGUF trompeur
    (llm_profiles.yaml déclarait `download: ollama_pull` sans que l'UI ne l'utilise —
    constat de l'analyse installation 2026-08-06)."""

    def _cfg(self, model_id="local/qwen3.6:27b", **services) -> dict:
        return {
            "models": {"stt_backend": "whisper", "diarization_backend": "sortformer"},
            "services": {"backend": "ollama", **services},
            "workflow": {"arbitration_llm": {"model_id": model_id}},
        }

    def test_ligne_ollama_remplace_le_gguf(self):
        specs = {s.role: s for s in build_catalog(self._cfg(), total_vram_mb=64000)}
        llm = specs["arbitrage_llm"]
        assert llm.kind == "ollama" and llm.repo_id == "qwen3.6:27b"
        assert "Ollama" in llm.label and llm.file is None and llm.gated is False

    def test_ollama_model_explicite_prime(self):
        specs = build_catalog(self._cfg(ollama_model="qwen3.5:9b"), total_vram_mb=64000)
        llm = next(s for s in specs if s.role == "arbitrage_llm")
        assert llm.repo_id == "qwen3.5:9b"

    def test_sans_model_resoluble_pas_de_ligne(self):
        specs = build_catalog(self._cfg(model_id=""), total_vram_mb=64000)
        assert not [s for s in specs if s.role == "arbitrage_llm"]

    def test_backend_llamacpp_inchange(self):
        cfg = self._cfg()
        cfg["services"]["backend"] = "http"
        llm = next(s for s in build_catalog(cfg, total_vram_mb=64000) if s.role == "arbitrage_llm")
        assert llm.kind == "gguf"

    def _spec(self) -> ModelSpec:
        return ModelSpec("arbitrage_llm", "LLM (Ollama : qwen3.6:27b)", "qwen3.6:27b",
                         None, "ollama", "", False, "l", "u", 0.0)

    def test_statut_present_via_tags_avec_tolerance_latest(self, tmp_path: Path):
        tags = [{"name": "qwen3.6:27b", "size": 17_000_000_000}]
        st = model_status(self._spec(), hf_home=tmp_path, models_dir=tmp_path, ollama_tags=tags)
        assert st == {"present": True, "path": None, "size_bytes": 17_000_000_000, "daemon_up": True}

    def test_statut_absent_daemon_up(self, tmp_path: Path):
        st = model_status(self._spec(), hf_home=tmp_path, models_dir=tmp_path,
                          ollama_tags=[{"name": "gemma4:12b", "size": 1}])
        assert st["present"] is False and st["daemon_up"] is True

    def test_statut_daemon_injoignable_distingue(self, tmp_path: Path):
        # None = /api/tags injoignable — l'UI doit distinguer « pas tiré » de « Ollama arrêté ».
        st = model_status(self._spec(), hf_home=tmp_path, models_dir=tmp_path, ollama_tags=None)
        assert st["present"] is False and st["daemon_up"] is False

    def test_catalog_with_status_sonde_les_tags_seulement_si_ollama(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mc, "fetch_ollama_tags", lambda cfg, **k: calls.append(1) or [])
        catalog_with_status(_cfg(), total_vram_mb=None)          # backend http : pas de sonde
        assert calls == []
        catalog_with_status(self._cfg(), total_vram_mb=None)     # backend ollama : une sonde
        assert calls == [1]


class TestOllamaModelChoices:
    """Liste de bascule Ollama : paliers atteignables (catalogue réel) + modèles tirés."""

    def _cfg(self, model="qwen3.5:9b"):
        return {"services": {"backend": "ollama", "ollama_model": model}}

    def _choices(self, tags=None, per_card=24500, count=1, model="qwen3.5:9b"):
        from transcria.models_catalog import ollama_model_choices
        return ollama_model_choices(self._cfg(model), tags, gpu_count=count,
                                    per_card_vram_mb=per_card, total_vram_mb=per_card * count)

    def test_paliers_dedupliques_et_recommandation(self):
        # 24,5 Go mono-GPU : paliers 12/16 (même modèle 9b, UNE ligne) + 24 (27b, recommandé).
        choices = self._choices()
        assert [c["model"] for c in choices] == ["qwen3.5:9b", "qwen3.6:27b"]
        assert [c["recommended"] for c in choices] == [False, True]
        assert choices[0]["active"] is True          # modèle configuré

    def test_vram_insuffisante_aucun_palier(self):
        assert self._choices(per_card=8000) == []

    def test_tags_marquent_tires_avec_taille(self):
        tags = [{"name": "qwen3.6:27b", "size": 17_000_000_000}]
        by_model = {c["model"]: c for c in self._choices(tags)}
        assert by_model["qwen3.6:27b"]["pulled"] is True
        assert by_model["qwen3.6:27b"]["size_bytes"] == 17_000_000_000
        assert by_model["qwen3.5:9b"]["pulled"] is False

    def test_modele_tire_hors_catalogue_propose_aussi(self):
        tags = [{"name": "mistral:7b", "size": 4_000_000_000}]
        choices = self._choices(tags)
        extra = next(c for c in choices if c["model"] == "mistral:7b")
        assert extra["pulled"] is True and extra["recommended"] is False

    def test_tags_none_demon_injoignable_rien_de_tire(self):
        assert all(c["pulled"] is False for c in self._choices(None))

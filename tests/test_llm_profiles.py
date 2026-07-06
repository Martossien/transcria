"""Catalogue de profils LLM (données) + sélection pilotée matériel — GPU-free.

Vérifie que rien n'est hardcodé : les paliers/modèles viennent du YAML de données, et la
sélection « fait au mieux avec le matériel » (mono vs multi-GPU) par moteur.
"""
import textwrap

import pytest

from transcria.config.llm_profiles import (
    ProfileChoice,
    load_llm_profiles,
    select_profile,
    valid_tp,
)

_P = load_llm_profiles()


class TestCatalogue:
    def test_three_engines_present(self):
        assert set(_P["engines"]) == {"llamacpp", "ollama", "vllm"}

    def test_every_tier_has_model_and_context_no_hardcoded_size(self):
        for eng, spec in _P["engines"].items():
            for t in spec["tiers"]:
                assert t.get("model") and t.get("context"), (eng, t.get("id"))
                # AUCUNE taille de modèle en dur : l'empreinte est dérivée (llm_footprint).
                assert "footprint" not in t and "value_mb" not in t, (eng, t.get("id"))

    def test_llamacpp_anchored_on_bench_models(self):
        models = [t["model"]["file"] for t in _P["engines"]["llamacpp"]["tiers"]]
        assert models[0] == "Qwen3.5-9B-Q5_K_M.gguf"
        assert any("35B-A3B" in m for m in models)


class TestOverride:
    def test_config_override_path(self, tmp_path):
        f = tmp_path / "custom.yaml"
        f.write_text(textwrap.dedent("""
            schema_version: 1
            engines:
              ollama:
                select_by: per_card_then_total
                tiers:
                  - {id: "x", min_vram_mb: 0, context: 4096, model: "tiny:1b", footprint: {value_mb: 1, source: estimated}}
        """))
        cfg = {"workflow": {"arbitration_llm": {"profiles_file": str(f)}}}
        p = load_llm_profiles(cfg)
        assert list(p["engines"]) == ["ollama"]
        assert p["engines"]["ollama"]["tiers"][0]["model"] == "tiny:1b"


class TestValidTp:
    def test_largest_valid_le_gpu_count(self):
        assert valid_tp(8, [1, 2, 4, 8]) == 8
        assert valid_tp(6, [1, 2, 4, 8]) == 4    # 8 > 6 → 4
        assert valid_tp(1, [1, 2, 4, 8]) == 1
        assert valid_tp(3, [2, 4]) == 2


class TestSelectLlamacpp:
    def test_total_vram_picks_top_tier(self):
        c = select_profile(_P, "llamacpp", gpu_count=8, per_card_vram_mb=24000, total_vram_mb=192000)
        assert c.tier_id == "64" and "35B-A3B" in c.model["file"] and c.gpus == 3

    def test_single_24gb(self):
        c = select_profile(_P, "llamacpp", gpu_count=1, per_card_vram_mb=24000, total_vram_mb=24000)
        assert c.tier_id == "24"     # 35B-A3B IQ4 mono-carte (tensor-split=1)

    def test_below_minimum_returns_none(self):
        assert select_profile(_P, "llamacpp", gpu_count=1, per_card_vram_mb=8000, total_vram_mb=8000) is None


class TestSelectOllama:
    def test_mono_gpu_uses_per_card(self):
        # 1 carte 24 Go → palier 24 (27b), pas de spread.
        c = select_profile(_P, "ollama", gpu_count=1, per_card_vram_mb=24000, total_vram_mb=24000)
        assert c.tier_id == "24" and c.model == "qwen3.6:27b"
        assert c.engine_env == {} and c.multi_gpu is False

    def test_multi_gpu_uses_total_and_enables_spread(self):
        # 8×24 Go → sélection par TOTAL (192 Go) → palier 64 (35b) + spread activé.
        c = select_profile(_P, "ollama", gpu_count=8, per_card_vram_mb=24000, total_vram_mb=192000)
        assert c.tier_id == "64" and c.model == "qwen3.6:35b"
        assert c.multi_gpu is True and c.engine_env.get("OLLAMA_SCHED_SPREAD") == "1"

    def test_small_single_card_downshifts(self):
        c = select_profile(_P, "ollama", gpu_count=1, per_card_vram_mb=12000, total_vram_mb=12000)
        assert c.tier_id == "12" and c.model == "qwen3.5:9b"


class TestSelectVllm:
    def test_tp_auto_from_gpu_count(self):
        c = select_profile(_P, "vllm", gpu_count=4, per_card_vram_mb=24000, total_vram_mb=96000)
        assert c.tier_id == "96" and c.tp == 4 and "FP8" in c.model

    def test_tp_clamped_to_gpu_count(self):
        # 96 Go via 8 cartes de 12 Go : palier 96 (tp tier=4), auto ≤ nb GPU (8) → reste 4.
        c = select_profile(_P, "vllm", gpu_count=8, per_card_vram_mb=12000, total_vram_mb=96000)
        assert c.tp == 4

    def test_returns_profilechoice(self):
        c = select_profile(_P, "vllm", gpu_count=2, per_card_vram_mb=24000, total_vram_mb=48000)
        assert isinstance(c, ProfileChoice) and c.tier_id == "48"


def test_unknown_engine_raises():
    with pytest.raises(KeyError):
        select_profile(_P, "sglang", gpu_count=1, per_card_vram_mb=24000, total_vram_mb=24000)


class TestVllmEnvResolver:
    def test_renders_env_from_choice(self):
        from transcria.install_arbitrage import render_vllm_env_shell

        c = select_profile(_P, "vllm", gpu_count=4, per_card_vram_mb=24000, total_vram_mb=96000)
        rendered = render_vllm_env_shell(c)
        assert "ARBITRAGE_MODEL='Qwen/Qwen3.6-27B-FP8'" in rendered
        assert "ARBITRAGE_TP=4" in rendered
        assert "ARBITRAGE_MAX_LEN=262144" in rendered

    def test_none_renders_empty(self):
        from transcria.install_arbitrage import render_vllm_env_shell

        assert render_vllm_env_shell(None).count("=\n") == 3   # 3 clés vides, sûr pour eval shell


class TestRecommendEngine:
    """C2.1 — recommandation de moteur pilotée par les données (jamais imposée)."""

    def _profiles(self):
        from transcria.config.llm_profiles import load_llm_profiles
        return load_llm_profiles(None)

    def test_petit_palier_recommande_llamacpp(self):
        from transcria.config.llm_profiles import recommend_engine
        rec = recommend_engine(self._profiles(), gpu_count=1,
                               per_card_vram_mb=12288, total_vram_mb=12288)
        assert rec["engine"] == "llamacpp"
        assert "Qwen3.5-9B" in rec["reason"]          # comparaison CONCRÈTE
        assert "qwen3.5:9b" in rec["reason"]
        assert "déconseillé" in rec["reason"]

    def test_grand_palier_recommande_ollama(self):
        from transcria.config.llm_profiles import recommend_engine
        rec = recommend_engine(self._profiles(), gpu_count=1,
                               per_card_vram_mb=49152, total_vram_mb=49152)
        assert rec["engine"] == "ollama"
        assert "sans compilation" in rec["reason"]

    def test_les_deux_choix_sont_exposes(self):
        from transcria.config.llm_profiles import recommend_engine
        rec = recommend_engine(self._profiles(), gpu_count=2,
                               per_card_vram_mb=24576, total_vram_mb=49152)
        assert rec["llamacpp"] is not None and rec["ollama"] is not None

    def test_catalogue_sans_bloc_recommandation(self):
        from transcria.config.llm_profiles import recommend_engine
        profiles = {k: v for k, v in self._profiles().items() if k != "engine_recommendation"}
        rec = recommend_engine(profiles, gpu_count=1,
                               per_card_vram_mb=12288, total_vram_mb=12288)
        assert rec["engine"] == "ollama"              # sans règle : défaut simple
        assert rec["reason"] == ""

"""Dérivation de l'empreinte VRAM (llm_footprint) — PUR, sans GPU ni réseau.

Vérifie que la taille n'est PAS hardcodée : KV calculé par formule, poids = taille de fichier
réelle, archi lue depuis config.json / GGUF. C'est ce qui rend le placement VRAM correct.
"""
from transcria.gpu.llm_footprint import (
    arch_from_hf_config,
    derive_footprint_mb,
    footprint_mb,
    kv_cache_mb,
    weights_mb,
)


class TestKvCache:
    def test_formula_scales_with_context(self):
        arch = dict(n_layers=48, n_kv_heads=8, head_dim=128, kv_dtype_bytes=2)
        small = kv_cache_mb(context=32768, **arch)
        big = kv_cache_mb(context=262144, **arch)
        assert big == small * 8              # KV linéaire en contexte
        assert small > 0

    def test_q8_kv_half_of_fp16(self):
        base = dict(n_layers=48, n_kv_heads=8, head_dim=128, context=262144)
        assert kv_cache_mb(kv_dtype_bytes=1, **base) * 2 == kv_cache_mb(kv_dtype_bytes=2, **base)

    def test_zero_on_bad_input(self):
        assert kv_cache_mb(n_layers=0, n_kv_heads=8, head_dim=128, context=1000, kv_dtype_bytes=2) == 0


class TestWeights:
    def test_file_size(self, tmp_path):
        f = tmp_path / "m.gguf"
        f.write_bytes(b"x" * (5 * 1024 * 1024))     # 5 Mo
        assert weights_mb(f) == 5

    def test_dir_sum(self, tmp_path):
        (tmp_path / "a.safetensors").write_bytes(b"x" * (3 * 1024 * 1024))
        (tmp_path / "b.safetensors").write_bytes(b"x" * (2 * 1024 * 1024))
        assert weights_mb(tmp_path) == 5

    def test_missing_is_zero(self, tmp_path):
        assert weights_mb(tmp_path / "nope") == 0


class TestArchFromHfConfig:
    def test_gqa_with_explicit_head_dim(self):
        cfg = {"num_hidden_layers": 48, "num_attention_heads": 40, "num_key_value_heads": 8, "head_dim": 128}
        assert arch_from_hf_config(cfg) == {"n_layers": 48, "n_kv_heads": 8, "head_dim": 128}

    def test_head_dim_from_hidden(self):
        cfg = {"num_hidden_layers": 36, "num_attention_heads": 32, "hidden_size": 4096}
        a = arch_from_hf_config(cfg)
        assert a == {"n_layers": 36, "n_kv_heads": 32, "head_dim": 128}     # MHA (kv=heads), 4096/32

    def test_incomplete_returns_none(self):
        assert arch_from_hf_config({"num_hidden_layers": 48}) is None
        assert arch_from_hf_config({}) is None


class TestFootprintAssembly:
    def test_footprint_adds_overhead(self):
        assert footprint_mb(10000, 5000, overhead_pct=0.10) == 16500

    def test_derive_combines_weights_and_kv(self, tmp_path):
        f = tmp_path / "m.gguf"
        f.write_bytes(b"x" * (6000 * 1024 * 1024 // 1000 * 1000))  # ~ quelques Go
        arch = {"n_layers": 48, "n_kv_heads": 8, "head_dim": 128}
        got = derive_footprint_mb(model_path=f, arch=arch, context=262144, kv_dtype_bytes=1)
        assert got is not None and got > weights_mb(f)              # poids + KV + marge

    def test_derive_none_without_weights_or_arch(self, tmp_path):
        assert derive_footprint_mb(model_path=None, arch={"n_layers": 1, "n_kv_heads": 1, "head_dim": 1},
                                   context=1000, kv_dtype_bytes=2) is None
        assert derive_footprint_mb(model_path=tmp_path / "x.gguf", arch=None,
                                   context=1000, kv_dtype_bytes=2) is None

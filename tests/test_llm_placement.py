"""Planification du placement LLM d'arbitrage (transcria.gpu.llm_placement).

Le cœur de ces tests = le **tableau de résistance** sur tout l'univers des cartes
NVIDIA : on vérifie que `recommend()` ne propose JAMAIS un placement qui OOM (le bug
historique : palier choisi sur la VRAM totale, profil mono-GPU appliqué à des cartes
trop petites, ou split égal débordant la plus petite carte hétérogène).

Tailles en Mio (réelles) : 8 Go=8192, 12=12288, 16=16384, 24=24576, 32=32607 (RTX 5090),
48=49152, 80=81920.
"""
from __future__ import annotations

import pytest

from transcria.gpu.llm_placement import (
    TIERS_BY_GB,
    evaluate_calibration,
    plan_for_tier,
    recommend,
)

MB_8, MB_12, MB_16, MB_24, MB_32, MB_48, MB_80 = 8192, 12288, 16384, 24576, 32607, 49152, 81920


class TestRecommendSingleCard:
    @pytest.mark.parametrize(
        "size,expected_tier",
        [
            (MB_8, 0),    # 8 Go : aucun modèle mono ne tient → transcription brute
            (MB_12, 12),  # 12 Go : palier 12 mono
            (MB_16, 16),  # 16 Go : palier 16 mono
            (MB_24, 24),  # 24 Go : palier 24 mono (35B 4-bit)
            (MB_32, 24),  # 1× 5090 : palier 24 (les paliers splités exigent ≥2 cartes)
            (MB_48, 24),  # 1× A6000 : idem — pas de profil mono au-delà de 24
            (MB_80, 24),  # 1× A100 : idem
        ],
    )
    def test_single_card_never_overcommits(self, size, expected_tier):
        p = recommend([size])
        assert p.tier_gb == expected_tier
        if expected_tier:
            assert p.feasible
            assert p.gpu_indices == [0]
            assert p.vram_mb + 1500 <= size  # marge respectée


class TestRecommendHomogeneousMulti:
    def test_two_8gb_is_raw_but_hints_a_custom_split(self):
        # 2× 8 Go : le modèle 16 tiendrait en split (6,35 Go/carte) mais profil mono only.
        p = recommend([MB_8, MB_8])
        assert not p.feasible and p.tier_gb == 0
        assert any("split" in w and "personnalisé" in w for w in p.warnings)

    def test_two_24gb_picks_48_split(self):
        # Banc mainteneur 2× 3090 : palier 48 (Q6) réparti sur 2 cartes.
        p = recommend([MB_24, MB_24])
        assert p.feasible and p.tier_gb == 48
        assert p.gpu_indices == [0, 1]
        assert sum(p.vram_mb_per_gpu) == p.vram_mb

    def test_three_24gb_picks_64_split(self):
        # Banc mainteneur 3× 3090 : palier 64 (Q8) réparti sur 3 cartes.
        p = recommend([MB_24, MB_24, MB_24])
        assert p.feasible and p.tier_gb == 64
        assert p.gpu_indices == [0, 1, 2]

    def test_two_5090_picks_48_not_broken_64(self):
        # 2× RTX 5090 (64 Go total) : le profil 64 exige 3 cartes → on retombe sur 48
        # (2 cartes), au lieu du placement [0,1,2] cassé qu'écrivait l'install par somme.
        p = recommend([MB_32, MB_32])
        assert p.feasible and p.tier_gb == 48
        assert p.gpu_indices == [0, 1]
        assert max(p.vram_mb_per_gpu) + 1500 <= MB_32

    def test_two_16gb_picks_32_split(self):
        p = recommend([MB_16, MB_16])
        assert p.feasible and p.tier_gb == 32
        assert max(p.vram_mb_per_gpu) + 1500 <= MB_16

    def test_four_16gb_uses_only_two_and_warns(self):
        p = recommend([MB_16, MB_16, MB_16, MB_16])
        assert p.feasible and p.tier_gb == 32  # profil 2 cartes
        assert p.gpu_indices == [0, 1]
        assert any("ignorées" in w for w in p.warnings)


class TestRecommendHeterogeneous:
    def test_8_plus_24_rejects_split_that_would_oom_small_card(self):
        # Le bug classique : 32 Go total via 8+24, split égal = 14,6 Go sur la carte 8 → OOM.
        p = recommend([MB_24, MB_8])
        # Aucun palier splité ne tient sur la carte de 8 ; mono-24 ne tient pas sur 8 non plus
        # MAIS la plus grande carte (24) héberge le palier 24 en mono.
        assert p.feasible and p.tier_gb == 24
        assert p.gpu_indices == [0]  # la carte de 24 (index 0)

    def test_8_at_index0_plus_24_at_index1_warns_about_arbitrage_gpu(self):
        p = recommend([MB_8, MB_24])
        assert p.feasible and p.tier_gb == 24
        assert p.gpu_indices == [1]
        assert any("ARBITRAGE_GPU=1" in w for w in p.warnings)

    def test_16_plus_24_fits_32_split_tightly(self):
        p = recommend([MB_24, MB_16])
        assert p.feasible and p.tier_gb == 32
        assert any("hétérogènes" in w for w in p.warnings)


class TestPlanForTier:
    def test_unknown_tier_is_infeasible(self):
        p = plan_for_tier(99, [MB_24])
        assert not p.feasible and "inconnu" in p.reason

    def test_no_gpu_is_infeasible(self):
        assert not plan_for_tier(24, []).feasible

    def test_invalid_size_is_rejected(self):
        assert not plan_for_tier(24, [0]).feasible
        assert not plan_for_tier(24, [-1000]).feasible

    def test_multi_gpu_tier_needs_enough_cards(self):
        p = plan_for_tier(64, [MB_24, MB_24])  # 64 exige 3 cartes
        assert not p.feasible and "3 cartes" in p.reason

    def test_split_shares_sum_to_footprint(self):
        p = plan_for_tier(48, [MB_24, MB_24])
        assert p.feasible
        assert sum(p.vram_mb_per_gpu) == p.vram_mb
        assert len(p.vram_mb_per_gpu) == 2

    def test_old_pascal_24gb_still_fits_24_tier_by_vram(self):
        # La compat archi (flash-attn) est un avertissement côté install, pas une question
        # de VRAM : côté placement, une P40 24 Go héberge bien le palier 24.
        p = plan_for_tier(24, [MB_24])
        assert p.feasible


class TestEvaluateCalibration:
    def _totals(self, *cards):
        return {i: c for i, c in enumerate(cards)}

    def test_conform_calibration_is_ok(self):
        report = evaluate_calibration(
            declared_indices=[0, 1],
            declared_vram_mb=48000,
            declared_per_gpu=[26000, 23000],
            observed_per_gpu={0: 25800, 1: 22900},
            free_per_gpu={0: 6800, 1: 9700},
            total_per_gpu=self._totals(32607, 32607),
        )
        assert report.ok
        assert report.suggested_vram_mb_per_gpu == [25800, 22900]

    def test_drift_is_flagged(self):
        report = evaluate_calibration(
            declared_indices=[0, 1],
            declared_vram_mb=48000,
            declared_per_gpu=[26000, 23000],
            observed_per_gpu={0: 18000, 1: 16000},  # ~30% sous le déclaré
            free_per_gpu={0: 14000, 1: 16000},
            total_per_gpu=self._totals(32607, 32607),
        )
        assert not report.ok
        assert any("dérive" in w for w in report.warnings)

    def test_critical_margin_is_flagged(self):
        report = evaluate_calibration(
            declared_indices=[0],
            declared_vram_mb=22300,
            declared_per_gpu=[22300],
            observed_per_gpu={0: 22300},
            free_per_gpu={0: 200},  # le cas Q4_K_M rejeté au bench
            total_per_gpu=self._totals(24576),
        )
        assert not report.ok
        assert any("OOM" in w for w in report.warnings)
        assert any(g.level == "critical" for g in report.per_gpu)

    def test_declared_card_without_llm_is_flagged(self):
        report = evaluate_calibration(
            declared_indices=[0, 1],
            declared_vram_mb=48000,
            declared_per_gpu=[24000, 24000],
            observed_per_gpu={0: 48000},  # tout sur une seule carte
            free_per_gpu={0: 1000, 1: 32000},
            total_per_gpu=self._totals(49152, 49152),
        )
        assert not report.ok
        assert any("placement incohérent" in w for w in report.warnings)

    def test_overflow_on_undeclared_card_is_flagged(self):
        report = evaluate_calibration(
            declared_indices=[0],
            declared_vram_mb=22300,
            declared_per_gpu=[22300],
            observed_per_gpu={0: 20000, 1: 5000},  # déborde sur GPU 1 non déclaré
            free_per_gpu={0: 4000, 1: 27000},
            total_per_gpu=self._totals(24576, 32607),
        )
        assert not report.ok
        assert any("PAS dans llm_gpu_indices" in w for w in report.warnings)

    def test_per_gpu_falls_back_to_equal_share_when_absent(self):
        report = evaluate_calibration(
            declared_indices=[0, 1],
            declared_vram_mb=48000,
            declared_per_gpu=None,  # pas de per_gpu → 24000/carte attendu
            observed_per_gpu={0: 24100, 1: 23900},
            free_per_gpu={0: 8000, 1: 8000},
            total_per_gpu=self._totals(32607, 32607),
        )
        assert report.ok  # dérive < 15%


class TestTierTable:
    def test_every_tier_split_is_consistent(self):
        for tier in TIERS_BY_GB.values():
            assert tier.footprint_mb > 0
            assert tier.profile_gpus >= 1
            assert tier.ctx in (196608, 262144)

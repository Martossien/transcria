"""Modèle de temps calibré machine — logique pure (régression, ratio, repli, format)."""
from __future__ import annotations

from transcria.workflow.timing_model import (
    Estimate,
    estimate_machine,
    estimate_stage,
    format_duration_fr,
    format_range_fr,
    human_review_minutes,
    legacy_machine_seconds,
)


class TestFallback:
    def test_formule_historique(self):
        # (600×0.35 + 130) × 1.25 = (210+130)×1.25 = 425
        assert legacy_machine_seconds(600) == 425.0
        assert legacy_machine_seconds(0) == 0.0

    def test_temps_humain(self):
        assert human_review_minutes(0) == 0
        assert human_review_minutes(1800) == 5      # 30 min → 5
        assert human_review_minutes(1801) == 10     # 30 min + 1 s → 2 tranches


class TestEstimateStage:
    def test_sans_historique_est_initial(self):
        e = estimate_stage([], 600)
        assert e.basis == "initial" and e.samples == 0 and e.seconds == 0.0

    def test_ratio_median_un_seul_point(self):
        # 1 point : 300 s d'audio → 150 s ⇒ ratio 0.5 ; sur 600 s → 300 s
        e = estimate_stage([(300, 150)], 600)
        assert e.basis == "measured" and e.samples == 1
        assert abs(e.seconds - 300) < 1e-6

    def test_regression_lineaire_pente_ordonnee(self):
        # durée = 0.5·audio + 60 (pente+ordonnée) ; 6 points parfaits → fit exact
        pts = [(a, 0.5 * a + 60) for a in (120, 240, 360, 480, 600, 720)]
        e = estimate_stage(pts, 1000)
        assert e.basis == "measured"
        assert abs(e.seconds - (0.5 * 1000 + 60)) < 1e-3
        # points parfaitement alignés → résidu ~0 → fourchette resserrée
        assert abs(e.high_seconds - e.low_seconds) < 1.0

    def test_estimation_jamais_negative(self):
        # ordonnée négative + petit audio ne doit pas donner un temps < 0
        pts = [(a, max(0.0, 0.5 * a - 500)) for a in (600, 700, 800, 900, 1000)]
        e = estimate_stage(pts, 10)
        assert e.seconds >= 0.0

    def test_fenetre_glissante_ignore_les_vieux(self):
        # 50 points « lents » puis assez de « rapides » ne sont pas testés ici en détail,
        # mais l'estimation reste finie et positive
        pts = [(600, 600)] * 60
        e = estimate_stage(pts, 600)
        assert e.seconds > 0


class TestEstimateMachine:
    def _hist(self, ratio):
        return [(a, ratio * a) for a in (120, 240, 360, 480, 600)]

    def test_calibre_si_toutes_les_etapes_ont_de_lhistorique(self):
        samples = {"transcribe": self._hist(0.3), "diarization": self._hist(0.1)}
        e = estimate_machine(["transcribe", "diarization"], samples, 600)
        assert e.basis == "measured"
        # 0.3·600 + 0.1·600 = 240
        assert abs(e.seconds - 240) < 1e-3

    def test_repli_formule_si_une_etape_sans_historique(self):
        samples = {"transcribe": self._hist(0.3)}  # diarization absente
        e = estimate_machine(["transcribe", "diarization"], samples, 600)
        assert e.basis == "initial"
        assert abs(e.seconds - legacy_machine_seconds(600)) < 1e-6

    def test_audio_nul(self):
        e = estimate_machine(["transcribe"], {}, 0)
        assert e.seconds == 0.0 and e.basis == "initial"


class TestFormat:
    def test_duree_fr(self):
        assert format_duration_fr(None) == "—"
        assert format_duration_fr(45) == "45 s"
        assert format_duration_fr(480) == "8 min"
        assert format_duration_fr(3900) == "1 h 05"

    def test_fourchette_resserree_vs_dispersee(self):
        assert format_range_fr(Estimate(480, "measured", 480, 480, 5)) == "~8 min"
        r = format_range_fr(Estimate(480, "measured", 360, 600, 5))
        assert "–" in r  # fourchette affichée

"""Fusion par piste (vague 5, lot B) — le module PUR qui transforme « N pistes
transcrites » en une timeline globale où les chevauchements ont leurs mots."""
from __future__ import annotations

from transcria.workflow.track_fusion import (
    fuse_track_segments,
    merge_windows,
    overlapping_indices,
)


class TestMergeWindows:
    def test_marge_et_fusion_des_fenetres_proches(self):
        # 2 fenêtres séparées de 1,2 s (< 2 s d'écart) → UN intervalle élargi de la marge.
        assert merge_windows([[1.0, 2.0], [3.2, 4.0]]) == [(0.6, 4.4)]

    def test_fenetres_lointaines_restent_separees(self):
        out = merge_windows([[0.0, 1.0], [10.0, 11.0]])
        assert out == [(0.0, 1.4), (9.6, 11.4)]

    def test_borne_haute_et_fenetres_invalides(self):
        out = merge_windows([[598.0, 601.0], ["x", 2], [5.0, 4.0]], max_end_s=600.0)
        assert out == [(597.6, 600.0)]            # bornée, les invalides écartées

    def test_le_levier_de_cout(self):
        """2 h de réunion, 10 min de parole → on ne transcrit QUE ~10 min."""
        windows = [[i * 60.0, i * 60.0 + 6.0] for i in range(100)]   # 100 × 6 s éparses
        total = sum(b - a for a, b in merge_windows(windows))
        assert total < 700                        # ~680 s transcrites, pas 7200

    def test_vide(self):
        assert merge_windows([]) == []
        assert merge_windows(None) == []


class TestFuseTrackSegments:
    def test_tri_global_et_chevauchement_conserve(self):
        """LE gain de la vague : deux locuteurs qui parlent EN MÊME TEMPS gardent chacun
        leurs mots, sur des intervalles qui se recouvrent."""
        alice = [{"start": 0.0, "end": 3.0, "text": "Bonjour à tous", "speaker": "Alice"}]
        bob = [{"start": 2.0, "end": 4.0, "text": "Pardon, je coupe", "speaker": "Bob"}]
        fused = fuse_track_segments([bob, alice])
        assert [s["speaker"] for s in fused] == ["Alice", "Bob"]
        assert fused[0]["end"] > fused[1]["start"]        # chevauchement INTACT
        assert {s["text"] for s in fused} == {"Bonjour à tous", "Pardon, je coupe"}

    def test_ordre_stable_a_depart_egal(self):
        a = [{"start": 1.0, "end": 2.0, "text": "a", "speaker": "B"}]
        b = [{"start": 1.0, "end": 2.0, "text": "b", "speaker": "A"}]
        assert [s["speaker"] for s in fuse_track_segments([a, b])] == ["A", "B"]

    def test_pistes_vides_tolerees(self):
        assert fuse_track_segments([[], None, [{"start": 0, "end": 1, "text": "x"}]]) \
            == [{"start": 0, "end": 1, "text": "x"}]


class TestOverlappingIndices:
    def test_chevauchement_entre_locuteurs_detecte(self):
        segs = [
            {"start": 0.0, "end": 3.0, "speaker": "Alice"},
            {"start": 2.0, "end": 4.0, "speaker": "Bob"},     # chevauche Alice
            {"start": 10.0, "end": 11.0, "speaker": "Alice"},  # isolé
        ]
        assert overlapping_indices(segs) == {0, 1}

    def test_meme_locuteur_ne_compte_pas(self):
        """Deux segments consécutifs du même locuteur qui se frôlent ne sont pas un
        chevauchement inter-locuteurs (le mix n'y est pas une bouillie)."""
        segs = [
            {"start": 0.0, "end": 3.0, "speaker": "Alice"},
            {"start": 2.5, "end": 5.0, "speaker": "Alice"},
        ]
        assert overlapping_indices(segs) == set()

    def test_segments_qui_se_touchent_sans_se_recouvrir(self):
        segs = [
            {"start": 0.0, "end": 2.0, "speaker": "Alice"},
            {"start": 2.0, "end": 4.0, "speaker": "Bob"},
        ]
        assert overlapping_indices(segs) == set()


class TestSubtractIntervals:
    def test_repisse_au_milieu_et_aux_bords(self):
        from transcria.workflow.track_fusion import subtract_intervals
        out = subtract_intervals([(0.0, 10.0), (20.0, 30.0)],
                                 [[2.0, 3.0], [9.0, 21.0]])
        assert out == [(0.0, 2.0), (3.0, 9.0), (21.0, 30.0)]

    def test_sans_trous_ni_invalides(self):
        from transcria.workflow.track_fusion import subtract_intervals
        assert subtract_intervals([(1.0, 2.0)], None) == [(1.0, 2.0)]
        assert subtract_intervals([(1.0, 2.0)], [[5.0, 4.0]]) == [(1.0, 2.0)]
        assert subtract_intervals([(1.0, 2.0)], [[0.0, 5.0]]) == []

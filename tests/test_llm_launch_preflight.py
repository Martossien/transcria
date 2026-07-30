"""Préflight VRAM du lancement de la LLM d'arbitrage + cohérence placement (doctor).

Incident du 2026-07-30 (job fc268816) : le script répartissait la LLM sur 3 cartes
(`--tensor-split 1,1,1`) mais la config déclarait `llm_gpu_indices: [0]` — l'allocateur ne
protégeait qu'une carte, une façade STT occupait 12 Go sur une carte du split, et
llama-server a SEGFAULTÉ (cudaMalloc OOM, code 139), panne muette diagnostiquée au core
dump. Deux gardes en sont nées, testées ici :
  1. `_preflight_llm_vram` — la VRAM RÉELLE des cartes du placement est vérifiée AVANT
     d'exécuter le script (libération sous pression, puis refus LISIBLE) ;
  2. `check_llm_placement_declaration` — le doctor dénonce la divergence statiquement.
"""
from __future__ import annotations

import pytest

from transcria.diagnostics.checks.common import OK, WARN
from transcria.diagnostics.checks.llm import (
    _tensor_split_card_count,
    check_llm_placement_declaration,
)
from transcria.gpu.vram_manager import VRAMManager


def _manager(gpu_cfg: dict, free_by_gpu: dict[int, int], monkeypatch) -> VRAMManager:
    mgr = VRAMManager({"gpu": {"min_free_vram_mb": 1000, **gpu_cfg}})
    monkeypatch.setattr(mgr, "get_free_vram_mb", lambda idx=0: free_by_gpu.get(idx, 0))
    return mgr


class TestLlmLaunchShares:
    def test_parts_depuis_la_liste_par_carte(self, monkeypatch):
        mgr = _manager({"llm_gpu_indices": [0, 1, 2],
                        "llm_vram_mb_per_gpu": [15000, 15000, 15000]}, {}, monkeypatch)
        assert mgr._llm_launch_shares() == {0: 15000, 1: 15000, 2: 15000}

    def test_partage_egal_du_total_a_defaut(self, monkeypatch):
        mgr = _manager({"llm_gpu_indices": [0, 1], "llm_vram_mb": 45000}, {}, monkeypatch)
        assert mgr._llm_launch_shares() == {0: 22500, 1: 22500}

    def test_sans_indices_aucune_part(self, monkeypatch):
        """On n'INVENTE jamais un placement (leçon du recalibrage du 2026-07-19)."""
        mgr = _manager({"llm_vram_mb": 45000}, {}, monkeypatch)
        assert mgr._llm_launch_shares() == {}


class TestPreflightLlmVram:
    CFG = {"llm_gpu_indices": [0, 1, 2], "llm_vram_mb_per_gpu": [15000, 15000, 15000]}

    def test_ok_quand_les_cartes_sont_libres(self, monkeypatch):
        mgr = _manager(self.CFG, {0: 24000, 1: 24000, 2: 24000}, monkeypatch)
        assert mgr._preflight_llm_vram() is True

    def test_refus_lisible_quand_une_carte_du_split_est_occupee(self, monkeypatch):
        """Le scénario EXACT de l'incident : 12 Go squattés sur la carte 2."""
        mgr = _manager(self.CFG, {0: 24000, 1: 24000, 2: 12300}, monkeypatch)
        monkeypatch.setattr("transcria.gpu.vram_manager.release_idle_vram", lambda: None)
        assert mgr._preflight_llm_vram() is False

    def test_liberation_sous_pression_puis_lancement(self, monkeypatch):
        """L'occupant est une façade inactive : la libération la décharge → feu vert."""
        free = {0: 24000, 1: 24000, 2: 12300}

        def _release():
            free[2] = 24000                      # la façade rend sa carte

        monkeypatch.setattr("transcria.gpu.vram_manager.release_idle_vram", _release)
        mgr = _manager(self.CFG, free, monkeypatch)
        assert mgr._preflight_llm_vram() is True

    def test_sans_placement_declare_comportement_historique(self, monkeypatch):
        mgr = _manager({}, {0: 0}, monkeypatch)
        assert mgr._preflight_llm_vram() is True

    def test_launch_refuse_sans_executer_le_script(self, tmp_path, monkeypatch):
        """Le refus arrive AVANT le Popen : plus jamais de segfault aveugle."""
        script = tmp_path / "launch.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        mgr = _manager(self.CFG, {0: 24000, 1: 24000, 2: 0}, monkeypatch)
        mgr.arbitrage_script = str(script)
        monkeypatch.setattr(mgr, "is_port_open", lambda port: False)
        monkeypatch.setattr("transcria.gpu.vram_manager.release_idle_vram", lambda: None)

        def _no_popen(*a, **k):  # pragma: no cover — ne doit JAMAIS être atteint
            pytest.fail("le script a été exécuté malgré le préflight en échec")

        monkeypatch.setattr("transcria.gpu.vram_manager.subprocess.Popen", _no_popen)
        assert mgr.launch_arbitrage_llm() is False


class TestTensorSplitParsing:
    @pytest.mark.parametrize("text,expected", [
        ("--tensor-split 1,1,1", 3),
        ("--tensor-split=2,1", 2),
        ("--tensor-split 0,1,1", 2),              # part nulle = carte non utilisée
        ("llama-server --port 8080", None),        # absent : indécidable statiquement
        ("--tensor-split 24,24,24,24 \\\n --autre", 4),
    ])
    def test_comptage(self, text, expected):
        assert _tensor_split_card_count(text) == expected


class TestCheckLlmPlacementDeclaration:
    def _cfg(self, indices):
        gpu = {"llm_gpu_indices": indices} if indices is not None else {}
        return {"services": {"arbitrage_script": "/x/launch.sh"}, "gpu": gpu}

    def test_divergence_denoncee(self):
        """Le cas de l'incident : split 3 cartes, config 1 carte → WARN actionnable."""
        res = check_llm_placement_declaration(
            self._cfg([0]), is_file=lambda p: True,
            read_text=lambda p: "--tensor-split 1,1,1")
        assert res.status == WARN
        assert "3" in res.detail and "1" in res.detail

    def test_placement_absent_denonce(self):
        res = check_llm_placement_declaration(
            self._cfg(None), is_file=lambda p: True,
            read_text=lambda p: "--tensor-split 1,1,1")
        assert res.status == WARN

    def test_alignement_ok(self):
        res = check_llm_placement_declaration(
            self._cfg([0, 1, 2]), is_file=lambda p: True,
            read_text=lambda p: "--tensor-split 1,1,1")
        assert res.status == OK

    def test_sans_split_indecidable_ok(self):
        res = check_llm_placement_declaration(
            self._cfg([0]), is_file=lambda p: True,
            read_text=lambda p: "llama-server --port 8080")
        assert res.status == OK

    def test_sans_script_sans_objet(self):
        res = check_llm_placement_declaration(
            self._cfg([0]), is_file=lambda p: False)
        assert res.status == OK

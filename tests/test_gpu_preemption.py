"""Gardes de préemption GPU (P1.a, audit 2026-07-30) — la frontière « owner ou pas ».

Trois protections nées de l'audit : les PID TRACKÉS (nos lancements) ne sont jamais
signalés par `_free_memory` ; la dérivation du fichier PID est PARTAGÉE entre
l'allocateur (écriture) et la préemption (lecture) ; et les kills par PORT respectent
enfin NEVER_KILL (le démon Ollama n'est jamais signalé, même squattant notre port).
"""
from __future__ import annotations

from transcria.gpu import _port_utils, pid_registry
from transcria.gpu.vram_manager import VRAMManager
from transcria.queue.allocator import GPUAllocator


class TestPidRegistryPartage:
    def test_meme_chemin_que_l_allocateur(self, tmp_path):
        """Deux dérivations divergentes feraient tuer nos propres processus."""
        cfg = {"workflow": {"scheduling": {"pid_file": str(tmp_path / "pids.json")}}}
        assert pid_registry.pid_file_path(cfg) == GPUAllocator(cfg)._pid_file

    def test_lit_ce_que_l_allocateur_persiste(self, tmp_path):
        cfg = {"workflow": {"scheduling": {"pid_file": str(tmp_path / "pids.json")}}}
        alloc = GPUAllocator(cfg)
        import os

        alloc.register_pid(os.getpid(), "arbitrage_llm")   # PID vivant (le nôtre)
        assert os.getpid() in pid_registry.tracked_pids(cfg)

    def test_fichier_absent_ensemble_vide(self, tmp_path):
        cfg = {"workflow": {"scheduling": {"pid_file": str(tmp_path / "absent.json")}}}
        assert pid_registry.tracked_pids(cfg) == set()


class TestFreeMemoryExclusions:
    _SMI = ("4242, llama-server, 16000\n"          # préemptable (pattern, gros)
            "5555, llama-server, 16000\n"          # TRACKÉ → épargné
            "6666, python3, 12000\n"               # hors patterns → épargné
            "7777, vllm::worker, 300\n")           # sous le seuil → épargné

    def _manager(self, monkeypatch, tmp_path):
        cfg = {"gpu": {}, "workflow": {"scheduling": {"pid_file": str(tmp_path / "pids.json")}}}
        mgr = VRAMManager(cfg)

        class _Result:
            stdout = self._SMI

        monkeypatch.setattr("transcria.gpu.vram_manager.subprocess.run",
                            lambda *a, **k: _Result())
        return mgr

    def test_seul_le_pattern_non_tracke_est_preemptable(self, monkeypatch, tmp_path):
        mgr = self._manager(monkeypatch, tmp_path)
        victims = mgr._preemptable_processes(0, protected={5555})
        assert victims == [(4242, "llama-server", 16000)]

    def test_free_memory_ne_signale_que_les_preemptables(self, monkeypatch, tmp_path):
        mgr = self._manager(monkeypatch, tmp_path)
        # Le PID 5555 est PERSISTÉ comme nôtre via le registre partagé.
        cfg = mgr.config
        import json

        pid_registry.pid_file_path(cfg).write_text(json.dumps({"5555": "arbitrage_llm"}))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr("transcria.gpu.vram_manager.os.kill",
                            lambda pid, sig: killed.append((pid, sig)))
        monkeypatch.setattr("transcria.gpu.vram_manager.time.sleep", lambda s: None)
        mgr._free_memory(0)
        assert {pid for pid, _ in killed} == {4242}


class TestKillPortNeverKill:
    def test_ollama_jamais_signale_meme_sur_notre_port(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(_port_utils, "_listening_pids", lambda port: [111, 222])
        monkeypatch.setattr(_port_utils, "_process_name",
                            lambda pid: "ollama" if pid == 111 else "llama-server")
        killed: list[int] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr(_port_utils.time, "sleep", lambda s: None)
        with caplog.at_level(logging.ERROR):
            assert _port_utils.kill_port_listeners(8080) is True
        assert 111 not in killed and 222 in killed
        assert any("ollama" in rec.getMessage() for rec in caplog.records)

    def test_port_libre_true_sans_kill(self, monkeypatch):
        monkeypatch.setattr(_port_utils, "_listening_pids", lambda port: [])
        killed: list[int] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
        assert _port_utils.kill_port_listeners(8080) is True
        assert killed == []

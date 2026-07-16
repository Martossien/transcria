"""Sonde GPU unique (gpu/inventory.py — vague B3).

GPU-free : torch est substitué dans sys.modules (l'import est différé DANS
snapshot()). On vérifie le contrat qui a motivé la fusion : les deux classes
(VRAMManager, GPUAllocator) lisent le MÊME inventaire, et une panne de sonde
vaut « aucun GPU » — jamais un crash (l'ancienne copie du manager laissait
passer les RuntimeError CUDA).
"""
import sys
from types import SimpleNamespace

from transcria.gpu import inventory
from transcria.gpu.inventory import GpuState, snapshot

_GIB = 1024 ** 3


def _fake_torch(mem_by_index, *, available=True):
    names = {i: f"NVIDIA GeForce RTX 509{i}" for i in mem_by_index}
    cuda = SimpleNamespace(
        is_available=lambda: available,
        device_count=lambda: len(mem_by_index),
        mem_get_info=lambda idx: mem_by_index[idx],
        get_device_name=lambda idx: names[idx],
    )
    return SimpleNamespace(cuda=cuda)


class TestSnapshot:
    def test_reports_each_card_in_gib(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch({
            0: (8 * _GIB, 32 * _GIB),      # (free, total)
            1: (30 * _GIB, 32 * _GIB),
        }))
        states = snapshot()
        assert [s.id for s in states] == [0, 1]
        assert states[0].free_gib == 8.0 and states[0].used_gib == 24.0 and states[0].total_gib == 32.0
        assert states[1].name == "NVIDIA GeForce RTX 5091"
        assert all(s.cuda_visible_remapped for s in states)

    def test_cuda_unavailable_is_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch({}, available=False))
        assert snapshot() == ()

    def test_probe_failure_is_empty_never_a_crash(self, monkeypatch):
        # L'ancienne copie de VRAMManager n'attrapait qu'ImportError : un RuntimeError
        # CUDA (driver en vrac, carte tombée) crashait l'admission. Politique unifiée.
        def boom(idx):
            raise RuntimeError("CUDA error: device-side assert triggered")

        fake = _fake_torch({0: (0, 0)})
        fake.cuda.mem_get_info = boom
        monkeypatch.setitem(sys.modules, "torch", fake)
        assert snapshot() == ()

    def test_as_dict_matches_historic_shape(self):
        state = GpuState(id=0, name="RTX", free_gib=8.0, used_gib=24.0, total_gib=32.0)
        assert state.as_dict() == {
            "id": 0,
            "name": "RTX",
            "cuda_visible_remapped": True,
            "memory": {"used": 24.0, "free": 8.0, "total": 32.0},
        }


class TestDelegation:
    """DoD B3 : les deux classes consomment la sonde — plus deux visions possibles."""

    _STATES = (GpuState(id=0, name="RTX", free_gib=8.0, used_gib=24.0, total_gib=32.0),)

    def test_both_classes_see_the_same_snapshot(self, monkeypatch, tmp_path):
        from builders import make_config

        from transcria.gpu.vram_manager import VRAMManager
        from transcria.queue.allocator import GPUAllocator

        monkeypatch.setattr(inventory, "snapshot", lambda: self._STATES)
        cfg = make_config(jobs_dir=tmp_path / "jobs")
        manager_view = VRAMManager(cfg).get_gpu_info()
        allocator_view = GPUAllocator(cfg).get_gpu_info()
        assert manager_view == allocator_view == [self._STATES[0].as_dict()]

"""Pré-lancement opt-in de la LLM d'arbitrage à l'étape ANALYSE (lot 2, §4.3-4 — gpu/llm_prelaunch).

Best-effort en thread : jamais de préemption (can_host requis), jamais de
lancement sans détenir le verrou LLM (discipline B3), défaut = comportement
historique (aucun pré-lancement).
"""
from __future__ import annotations

from transcria.gpu import llm_prelaunch


class _ImmediateThread:
    """Substitut de threading.Thread : exécute la cible tout de suite (déterministe)."""

    started = 0

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        type(self).started += 1
        self._target()


class _FakeAllocator:
    def __init__(self, *, lock_free=True, can_host=True):
        self.lock_free = lock_free
        self.can_host_value = can_host
        self.acquired = []
        self.released = []

    def try_acquire_llm(self, owner, timeout_s=0):
        if not self.lock_free:
            return False
        self.acquired.append(owner)
        return True

    def release_llm(self, owner):
        self.released.append(owner)

    def can_host_llm(self, total_mb):
        return self.can_host_value


class _FakeVram:
    def __init__(self, *, running=False):
        self.running = running
        self.launches = 0

    def is_arbitrage_llm_running(self):
        return self.running

    def launch_arbitrage_llm(self):
        self.launches += 1
        return True


def _wire(monkeypatch, allocator, vram):
    _ImmediateThread.started = 0
    monkeypatch.setattr(llm_prelaunch.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(llm_prelaunch.GPUAllocator, "get_instance",
                        classmethod(lambda cls, cfg=None: allocator))
    monkeypatch.setattr(llm_prelaunch, "VRAMManager", lambda cfg: vram)
    monkeypatch.setattr(llm_prelaunch, "is_remote_arbitrage", lambda cfg: False)


def _cfg(**llm) -> dict:
    return {"workflow": {"arbitration_llm": {"enabled": True, **llm}},
            "gpu": {"llm_vram_mb": 14700}}


class TestPrelaunch:
    def test_defaut_aucun_prelancement(self, monkeypatch):
        allocator, vram = _FakeAllocator(), _FakeVram()
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg())  # clé absente

        assert _ImmediateThread.started == 0
        assert vram.launches == 0

    def test_opt_in_lance_sous_verrou_puis_le_rend(self, monkeypatch):
        allocator, vram = _FakeAllocator(), _FakeVram()
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg(prelaunch_at_analyze=True))

        assert vram.launches == 1
        assert allocator.acquired == ["__prelaunch__"]
        assert allocator.released == ["__prelaunch__"]

    def test_verrou_occupe_aucun_lancement(self, monkeypatch):
        allocator, vram = _FakeAllocator(lock_free=False), _FakeVram()
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg(prelaunch_at_analyze=True))

        assert vram.launches == 0
        assert allocator.released == []  # jamais acquis → jamais rendu

    def test_deja_chaude_aucun_lancement(self, monkeypatch):
        allocator, vram = _FakeAllocator(), _FakeVram(running=True)
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg(prelaunch_at_analyze=True))

        assert vram.launches == 0
        assert allocator.released == ["__prelaunch__"]  # verrou pris puis rendu

    def test_vram_occupee_jamais_de_preemption(self, monkeypatch):
        allocator, vram = _FakeAllocator(can_host=False), _FakeVram()
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg(prelaunch_at_analyze=True))

        assert vram.launches == 0

    def test_arbitrage_distant_aucun_thread(self, monkeypatch):
        allocator, vram = _FakeAllocator(), _FakeVram()
        _wire(monkeypatch, allocator, vram)
        monkeypatch.setattr(llm_prelaunch, "is_remote_arbitrage", lambda cfg: True)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(_cfg(prelaunch_at_analyze=True))

        assert _ImmediateThread.started == 0

    def test_llm_coupee_explicitement_aucun_thread(self, monkeypatch):
        allocator, vram = _FakeAllocator(), _FakeVram()
        _wire(monkeypatch, allocator, vram)

        llm_prelaunch.maybe_prelaunch_arbitrage_llm(
            _cfg(enabled=False, prelaunch_at_analyze=True))

        assert _ImmediateThread.started == 0

"""Tests for VRAM Manager."""
import pytest

from transcria.gpu.vram_manager import VRAMManager


class TestVRAMManager:
    def test_instantiation(self):
        mgr = VRAMManager()
        assert mgr is not None
        assert mgr.dashboard_url == "http://127.0.0.1:5001"

    def test_custom_url(self):
        mgr = VRAMManager("http://10.0.0.1:9999")
        assert mgr.dashboard_url == "http://10.0.0.1:9999"

    def test_get_gpu_info(self):
        mgr = VRAMManager()
        gpus = mgr.get_gpu_info()
        assert isinstance(gpus, list)
        if gpus:
            g = gpus[0]
            assert "id" in g
            assert "memory" in g
            assert "free" in g["memory"]
            assert "total" in g["memory"]

    def test_get_free_vram_mb(self):
        mgr = VRAMManager()
        free = mgr.get_free_vram_mb(0)
        assert isinstance(free, int)
        assert free > 0

    def test_get_best_gpu(self):
        mgr = VRAMManager()
        best = mgr.get_best_gpu(100)
        assert best is None or isinstance(best, int)

    def test_ensure_free_returns_gpu(self):
        mgr = VRAMManager()
        result = mgr.ensure_free(50, preferred_gpu=0)
        assert result is None or isinstance(result, int)

    def test_track_untrack_model(self):
        mgr = VRAMManager()
        mgr.track_model("test-model", 0, 1000)
        assert "test-model" in mgr._loaded_models
        mgr.untrack_model("test-model")
        assert "test-model" not in mgr._loaded_models

    def test_offload_all(self):
        mgr = VRAMManager()
        mgr.track_model("m1", 0, 1000)
        mgr.track_model("m2", 1, 2000)
        mgr.offload_all()
        assert len(mgr._loaded_models) == 0

    def test_constants(self):
        assert VRAMManager.COHERE_VRAM_MB > 0
        assert VRAMManager.PYANNOTE_VRAM_MB > 0
        assert VRAMManager.MIN_FREE_MB > 0

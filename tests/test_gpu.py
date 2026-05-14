"""Tests for VRAM Manager."""
import pytest

from transcria.gpu.vram_manager import VRAMManager


def _default_config():
    return {
        "services": {
            "dashboard_llm_url": "http://127.0.0.1:5001",
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "qwen_port": 8080,
            "vllm_port": 8000,
        }
    }


class TestVRAMManager:
    def test_instantiation(self):
        mgr = VRAMManager(config=_default_config())
        assert mgr is not None
        assert mgr.dashboard_url == "http://127.0.0.1:5001"

    def test_custom_url(self):
        cfg = _default_config()
        cfg["services"]["dashboard_llm_url"] = "http://10.0.0.1:9999"
        mgr = VRAMManager(config=cfg)
        assert mgr.dashboard_url == "http://10.0.0.1:9999"

    def test_config_overrides(self):
        cfg = _default_config()
        cfg["services"]["qwen_port"] = 9999
        cfg["services"]["vllm_port"] = 8888
        mgr = VRAMManager(config=cfg)
        assert mgr.qwen_port == 9999
        assert mgr.vllm_port == 8888

    def test_script_paths_from_config(self):
        cfg = _default_config()
        mgr = VRAMManager(config=cfg)
        assert mgr.arbitrage_script == "/bin/true"
        assert mgr.stop_script == "/bin/true"

    def test_get_gpu_info(self):
        mgr = VRAMManager(config=_default_config())
        gpus = mgr.get_gpu_info()
        assert isinstance(gpus, list)
        if gpus:
            g = gpus[0]
            assert "id" in g
            assert "memory" in g
            assert "free" in g["memory"]
            assert "total" in g["memory"]

    def test_get_free_vram_mb(self):
        mgr = VRAMManager(config=_default_config())
        free = mgr.get_free_vram_mb(0)
        assert isinstance(free, int)
        assert free > 0

    def test_get_best_gpu(self):
        mgr = VRAMManager(config=_default_config())
        best = mgr.get_best_gpu(100)
        assert best is None or isinstance(best, int)

    def test_ensure_free_returns_gpu(self):
        mgr = VRAMManager(config=_default_config())
        result = mgr.ensure_free(50, preferred_gpu=0)
        assert result is None or isinstance(result, int)

    def test_track_untrack_model(self):
        mgr = VRAMManager(config=_default_config())
        mgr.track_model("test-model", 0, 1000)
        assert "test-model" in mgr._loaded_models
        mgr.untrack_model("test-model")
        assert "test-model" not in mgr._loaded_models

    def test_offload_all(self):
        mgr = VRAMManager(config=_default_config())
        mgr.track_model("m1", 0, 1000)
        mgr.track_model("m2", 1, 2000)
        mgr.offload_all()
        assert len(mgr._loaded_models) == 0

    def test_constants(self):
        assert VRAMManager.COHERE_VRAM_MB > 0
        assert VRAMManager.PYANNOTE_VRAM_MB > 0
        assert VRAMManager.MIN_FREE_MB > 0
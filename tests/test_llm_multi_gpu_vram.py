"""Gestion VRAM de la LLM d'arbitrage multi-GPU — audit du 11/06/2026.

La LLM (ex. 35B Q8 ≈ 60 Go) s'étale sur plusieurs cartes via son script de lancement :
la modéliser en réservation mono-GPU était insatisfaisable par construction (60 Go ne
tiennent JAMAIS dans une RTX 3090) → code mort tant que la LLM tournait, et deadlock
vram_wait dès qu'il fallait la relancer. Nouveau modèle : besoin PAR GPU
(total ÷ nb de cartes du placement `gpu.llm_gpu_indices`), tout-ou-rien.
"""
from __future__ import annotations

from transcria.queue.allocator import GPUAllocator


def _allocator(tmp_path, gpu_count=3, free_mb=23500, total_mb=24576, llm_indices=None):
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "workflow": {"scheduling": {"pid_file": str(tmp_path / "pids.json")}},
    }
    if llm_indices is not None:
        cfg["gpu"]["llm_gpu_indices"] = llm_indices
    alloc = GPUAllocator(cfg)
    alloc.get_gpu_info = lambda: [
        {
            "id": i,
            "name": f"GPU {i}",
            "memory": {"free": free_mb / 1024, "used": (total_mb - free_mb) / 1024, "total": total_mb / 1024},
        }
        for i in range(gpu_count)
    ]
    return alloc


class TestCanHostLlm:
    def test_qwen_35b_fits_across_three_24gb_gpus(self, tmp_path):
        """Le cas prod : 60 Go au total = 20 Go/GPU sur 3×24 Go → hébergeable."""
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        assert alloc.can_host_llm(60000) is True

    def test_qwen_35b_never_fits_single_gpu_model(self, tmp_path):
        """La preuve de l'ancien bug : en mono-GPU, 60 Go étaient insatisfaisables."""
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        assert alloc.can_allocate(60000) is None  # ancien modèle : jamais possible
        assert alloc.can_host_llm(60000) is True   # nouveau modèle : possible

    def test_refused_when_one_target_gpu_is_loaded(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        # GPU 1 occupé par une autre phase : 23500 - 5000 = 18500 < 20000 + 1000.
        assert alloc.try_reserve("autre-job", 5000, "stt", preferred_gpu=1) is not None
        assert alloc.can_host_llm(60000) is False

    def test_only_target_gpus_matter(self, tmp_path):
        """Un GPU hors placement peut être plein sans bloquer la LLM."""
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1])
        assert alloc.try_reserve("autre-job", 20000, "stt", preferred_gpu=2) is not None
        assert alloc.can_host_llm(30000) is True  # 15000/GPU sur 0 et 1

    def test_default_indices_use_all_visible_gpus(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=2)  # pas de llm_gpu_indices
        assert alloc._llm_gpu_indices() == [0, 1]
        assert alloc.can_host_llm(40000) is True  # 20000/GPU sur 2×24 Go

    def test_no_gpu_means_not_hostable(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=0)
        assert alloc.can_host_llm(16000) is False


class TestTryReserveLlm:
    def test_reserves_one_share_per_gpu(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        assert alloc.try_reserve_llm("job-llm", 60000, "llm_arbitration") is True
        for idx in (0, 1, 2):
            assert alloc.get_available_vram_mb(idx) == 23500 - 20000

    def test_all_or_nothing_on_partial_shortage(self, tmp_path):
        """Échec partiel = AUCUNE réservation laissée (pas de fuite comptable)."""
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        assert alloc.try_reserve("autre-job", 5000, "stt", preferred_gpu=2) is not None
        assert alloc.try_reserve_llm("job-llm", 60000, "llm_arbitration") is False
        # Les GPU 0 et 1 n'ont RIEN gardé de la tentative.
        assert alloc.get_available_vram_mb(0) == 23500
        assert alloc.get_available_vram_mb(1) == 23500

    def test_idempotent_per_job_and_phase(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=2, llm_indices=[0, 1])
        assert alloc.try_reserve_llm("job-llm", 30000, "summary_llm") is True
        assert alloc.try_reserve_llm("job-llm", 30000, "summary_llm") is True  # no-op
        assert alloc.get_available_vram_mb(0) == 23500 - 15000  # pas doublé

    def test_release_phase_frees_all_shares(self, tmp_path):
        alloc = _allocator(tmp_path, gpu_count=3, llm_indices=[0, 1, 2])
        alloc.try_reserve_llm("job-llm", 60000, "llm_arbitration")
        alloc.release_phase("job-llm", "llm_arbitration")
        for idx in (0, 1, 2):
            assert alloc.get_available_vram_mb(idx) == 23500


class TestSchedulerLlmAdmission:
    def _scheduler(self, app, tmp_path):
        from transcria.queue.scheduler import QueueScheduler
        cfg = {
            "storage": {"jobs_dir": str(tmp_path)},
            "gpu": {"min_free_vram_mb": 1000},
            "workflow": {"queue": {"enabled": True, "poll_interval_s": 300},
                         "scheduling": {"pid_file": str(tmp_path / "pids.json")}},
        }
        return QueueScheduler(app, cfg, lambda *a: None)

    def test_llm_running_means_shared_no_requirement(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path)
        monkeypatch.setattr(sched, "_vram_manager",
                            lambda: type("V", (), {"is_arbitrage_llm_running": lambda self: True})())
        monkeypatch.setattr(sched.allocator, "can_host_llm",
                            lambda mb: (_ for _ in ()).throw(AssertionError("ne doit pas être appelé")))
        profile = {"phases": {"stt": 6000, "llm_arbitration": 60000}}
        assert sched._llm_admissible(profile, set()) is True

    def test_llm_down_requires_multi_gpu_capacity(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path)
        monkeypatch.setattr(sched, "_vram_manager",
                            lambda: type("V", (), {"is_arbitrage_llm_running": lambda self: False})())
        profile = {"phases": {"stt": 6000, "llm_arbitration": 60000}}
        monkeypatch.setattr(sched.allocator, "can_host_llm", lambda mb: False)
        assert sched._llm_admissible(profile, set()) is False
        monkeypatch.setattr(sched.allocator, "can_host_llm", lambda mb: True)
        assert sched._llm_admissible(profile, set()) is True

    def test_llm_phase_done_or_absent_is_admissible(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path)
        monkeypatch.setattr(sched, "_vram_manager",
                            lambda: type("V", (), {"is_arbitrage_llm_running": lambda self: False})())
        assert sched._llm_admissible({"phases": {"stt": 6000}}, set()) is True
        profile = {"phases": {"stt": 6000, "llm_arbitration": 60000}}
        assert sched._llm_admissible(profile, {"llm_arbitration"}) is True

    def test_local_required_mb_never_counts_llm(self, app, tmp_path):
        """Le max mono-GPU ne doit JAMAIS inclure la LLM (besoin multi-GPU à part) —
        avant, sans `llm_shared`, l'admission exigeait 60000 sur UNE carte (impossible)."""
        sched = self._scheduler(app, tmp_path)
        profile = {"phases": {"stt": 6000, "diarization": 2000, "llm_arbitration": 60000}}
        assert sched._local_required_mb(profile, set()) == 6000

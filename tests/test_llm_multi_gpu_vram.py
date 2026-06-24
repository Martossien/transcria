"""Gestion VRAM de la LLM d'arbitrage multi-GPU — audit du 11/06/2026.

La LLM (ex. 35B Q8 ≈ 60 Go) s'étale sur plusieurs cartes via son script de lancement :
la modéliser en réservation mono-GPU était insatisfaisable par construction (60 Go ne
tiennent JAMAIS dans une RTX 3090) → code mort tant que la LLM tournait, et deadlock
vram_wait dès qu'il fallait la relancer. Nouveau modèle : besoin PAR GPU
(total ÷ nb de cartes du placement `gpu.llm_gpu_indices`), tout-ou-rien.
"""
from __future__ import annotations

from transcria.queue.allocator import GPUAllocator


def _allocator(tmp_path, gpu_count=3, free_mb=23500, total_mb=24576, llm_indices=None,
               per_gpu_free=None, llm_per_gpu=None):
    """`per_gpu_free` : liste de Mo libres par carte (cartes HÉTÉROGÈNES)."""
    cfg = {
        "gpu": {"min_free_vram_mb": 1000},
        "workflow": {"scheduling": {"pid_file": str(tmp_path / "pids.json")}},
    }
    if llm_indices is not None:
        cfg["gpu"]["llm_gpu_indices"] = llm_indices
    if llm_per_gpu is not None:
        cfg["gpu"]["llm_vram_mb_per_gpu"] = llm_per_gpu
    frees = per_gpu_free if per_gpu_free is not None else [free_mb] * gpu_count
    alloc = GPUAllocator(cfg)
    alloc.get_gpu_info = lambda: [
        {
            "id": i,
            "name": f"GPU {i}",
            "memory": {"free": f / 1024, "used": max(0, total_mb - f) / 1024, "total": total_mb / 1024},
        }
        for i, f in enumerate(frees)
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


class TestHeterogeneousGpus:
    """Les cartes ne font pas toutes 24 Go (2/4/8/12/16/32/48/64…) : la part de la LLM
    par carte peut être inégale (`--tensor-split 3,1`) et les petites phases doivent
    aller sur les cartes qui conviennent — pas sur celles du placement LLM."""

    def test_unequal_split_via_per_gpu_shares(self, tmp_path):
        """24 Go + 8 Go avec tensor-split inégal : la répartition égale échoue,
        les parts déclarées par carte passent."""
        alloc = _allocator(tmp_path, per_gpu_free=[23500, 7500], llm_indices=[0, 1])
        # Égal : 24000/2 = 12000 + 1000 marge > 7500 sur la petite carte → refus.
        assert alloc.can_host_llm(24000) is False
        alloc2 = _allocator(tmp_path, per_gpu_free=[23500, 7500], llm_indices=[0, 1],
                            llm_per_gpu=[18000, 6000])
        assert alloc2.can_host_llm(24000) is True
        assert alloc2.try_reserve_llm("job-llm", 24000, "llm_arbitration") is True
        assert alloc2.get_available_vram_mb(0) == 23500 - 18000
        assert alloc2.get_available_vram_mb(1) == 7500 - 6000

    def test_small_phase_lands_on_the_card_that_fits(self, tmp_path):
        """Carte 8 Go + carte 24 Go : un STT de 6000 va sur la 24 Go (6000+1000 > 7000...
        ici 6 Go libres sur la petite) — la mesure réelle par carte décide."""
        alloc = _allocator(tmp_path, per_gpu_free=[6000, 23500])
        reservation = alloc.try_reserve("job-stt", 6000, "stt")
        assert reservation is not None and reservation.gpu_index == 1

    def test_small_phase_prefers_non_llm_gpus(self, tmp_path):
        """Placement LLM explicite [0,1] : une petite phase va sur le GPU 2 même si
        les cartes LLM ont plus de VRAM libre (préserver la relance de la LLM)."""
        alloc = _allocator(tmp_path, per_gpu_free=[23500, 23500, 20000], llm_indices=[0, 1])
        reservation = alloc.try_reserve("job-stt", 6000, "stt")
        assert reservation is not None and reservation.gpu_index == 2

    def test_llm_gpu_used_when_others_full(self, tmp_path):
        """Mais si seules les cartes LLM conviennent, on les utilise (pas de famine)."""
        alloc = _allocator(tmp_path, per_gpu_free=[23500, 23500, 2000], llm_indices=[0, 1])
        reservation = alloc.try_reserve("job-stt", 6000, "stt")
        assert reservation is not None and reservation.gpu_index in (0, 1)


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


class TestProfileVramProfileAdmission:
    """Phase 3 : le vram_profile produit PAR PROFIL pilote l'admission sans toucher au scheduler.

    Invariant clé : un profil sans LLM/diarisation n'est jamais bloqué par ces ressources,
    car son vram_profile n'expose simplement pas la phase correspondante.
    """

    _EST_CFG = {
        "models": {"stt_backend": "cohere", "diarization_backend": "pyannote"},
        "gpu": {"llm_vram_mb": 60000},
        "workflow": {"arbitration_llm": {"enabled": True}},
    }

    def _scheduler(self, app, tmp_path):
        from transcria.queue.scheduler import QueueScheduler
        cfg = {
            "storage": {"jobs_dir": str(tmp_path)},
            "gpu": {"min_free_vram_mb": 1000},
            "workflow": {"queue": {"enabled": True, "poll_interval_s": 300},
                         "scheduling": {"pid_file": str(tmp_path / "pids.json")}},
        }
        return QueueScheduler(app, cfg, lambda *a: None)

    def _vram_profile(self, profile_id):
        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow.profiles import get_profile
        return PipelineService.estimate_profile_resources(self._EST_CFG, get_profile(profile_id))

    def test_profil_sans_llm_jamais_bloque_derriere_la_llm(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path)
        # LLM éteinte ET incapable d'héberger : un profil avec LLM serait refusé…
        monkeypatch.setattr(sched, "_vram_manager",
                            lambda: type("V", (), {"is_arbitrage_llm_running": lambda self: False})())
        monkeypatch.setattr(sched.allocator, "can_host_llm", lambda mb: False)
        # … mais srt_express n'a pas de phase LLM → admissible.
        assert sched._llm_admissible(self._vram_profile("srt_express"), set()) is True
        # word_rapide a une phase LLM (résumé) → reste gouverné par la capacité LLM.
        assert sched._llm_admissible(self._vram_profile("word_rapide"), set()) is False

    def test_profil_sans_diarisation_ne_reserve_pas_la_vram_diarisation(self, app, tmp_path):
        sched = self._scheduler(app, tmp_path)
        # srt_express : seul le STT compte dans le max mono-GPU.
        assert sched._local_required_mb(self._vram_profile("srt_express"), set()) == 6000
        # srt_locuteurs : pas de phase diarisation (locuteurs via wizard) → STT seul aussi.
        assert sched._local_required_mb(self._vram_profile("srt_locuteurs"), set()) == 6000

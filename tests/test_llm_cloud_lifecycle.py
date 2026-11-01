"""Cycle de vie de la LLM d'arbitrage en mode CLOUD (lot A) — sites d'appel, GPU-free.

Couvre : la règle de panne unique (une reprise automatique puis échec explicite, jamais
``vram_wait``) appliquée par les phases, l'admission sans part LLM, la récupération VRAM
et l'arrêt de fin de pipeline qui sortent en tête. Le mode LOCAL reste inchangé.
"""
import logging
from types import SimpleNamespace

import pytest
from builders import make_config, make_job_stub
from fakes import FakeArbitrageVram, FakeLlmLockAllocator

from transcria.gpu.vram_reclaim import stop_idle_arbitrage_llm
from transcria.jobs.filesystem import JobFilesystem
from transcria.services import pipeline_admission
from transcria.services.pipeline_service import PipelineService
from transcria.workflow import llm_readiness
from transcria.workflow.phases import correction, summary_llm
from transcria.workflow.profiles import get_profile, resolve_legacy_mode

_CLOUD = {
    "services": {"backend": "cloud"},
    "workflow": {
        "arbitration_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
        "summary_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
    },
}


def _cloud_cfg(tmp_path=None):
    return make_config(_CLOUD, jobs_dir=str(tmp_path / "jobs") if tmp_path else None)


def _local_cfg(tmp_path=None, **services):
    return make_config(
        {"services": dict(services), "workflow": {"arbitration_llm": {"enabled": True},
                                                  "summary_llm": {"enabled": True}}},
        jobs_dir=str(tmp_path / "jobs") if tmp_path else None,
    )


class _ScriptedVram:
    """``ensure_arbitrage_llm_ready`` rejoue des réponses ; toute sonde « running » est comptée."""

    def __init__(self, results, config=None):
        self.results = list(results)
        self.config = config or {}
        self.ensure_calls = 0
        self.running_calls = 0
        self.stop_calls = 0

    def ensure_arbitrage_llm_ready(self, expected_model_id=None):
        self.ensure_calls += 1
        return self.results.pop(0)

    def is_arbitrage_llm_running(self):
        self.running_calls += 1
        return False

    def stop_arbitrage_llm(self):
        self.stop_calls += 1
        return True


class _Progress:
    def update(self, *args, **kwargs):
        pass


def _runner(cfg, vram):
    jobs_dir = cfg["storage"]["jobs_dir"]
    return SimpleNamespace(
        progress=_Progress(),
        _get_fs=lambda _cfg, job_id: JobFilesystem(jobs_dir, job_id),
        allocator=FakeLlmLockAllocator(),
        vram=vram,
        _should_reserve_llm_vram=lambda: False,
    )


@pytest.fixture
def pauses(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(llm_readiness, "_pause", calls.append)
    return calls


def _job_with_srt(cfg):
    job = make_job_stub()
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")
    return job


# ---------------------------------------------------------------- helper partagé
class TestEnsureLlmReady:
    def test_local_un_seul_appel_sans_pause(self, pauses):
        vram = _ScriptedVram([False])
        assert llm_readiness.ensure_llm_ready(vram, _local_cfg(), "arbitrage") is False
        assert vram.ensure_calls == 1 and pauses == []

    def test_cloud_une_reprise_puis_echec(self, pauses):
        vram = _ScriptedVram([False, False])
        assert llm_readiness.ensure_llm_ready(vram, _cloud_cfg(), None) is False
        assert vram.ensure_calls == 2
        assert pauses == [llm_readiness.CLOUD_RETRY_DELAY_S]

    def test_cloud_reprise_reussie(self, pauses):
        vram = _ScriptedVram([False, True])
        assert llm_readiness.ensure_llm_ready(vram, _cloud_cfg(), None) is True
        assert vram.ensure_calls == 2 and len(pauses) == 1

    def test_cloud_succes_immediat_sans_pause(self, pauses):
        vram = _ScriptedVram([True])
        assert llm_readiness.ensure_llm_ready(vram, _cloud_cfg(), None) is True
        assert vram.ensure_calls == 1 and pauses == []

    def test_snapshot_running_sans_sonde_en_cloud(self):
        vram = _ScriptedVram([])
        assert llm_readiness.llm_running_before_phase(vram, _cloud_cfg()) is True
        assert vram.running_calls == 0
        assert llm_readiness.llm_running_before_phase(vram, _local_cfg()) is False
        assert vram.running_calls == 1

    def test_message_nomme_le_fournisseur(self):
        msg = llm_readiness.cloud_unavailable_error(_cloud_cfg())
        assert "LLM cloud mistral injoignable" in msg and "relançable" in msg


# ---------------------------------------------------------------- correction
class TestCorrectionPanneCloud:
    def test_echec_explicite_jamais_vram_wait(self, tmp_path, pauses):
        cfg = _cloud_cfg(tmp_path)
        vram = _ScriptedVram([False, False])
        result = correction.run(_runner(cfg, vram), _job_with_srt(cfg), cfg)
        assert result["success"] is False
        assert "vram_wait" not in result
        assert "LLM cloud mistral injoignable" in result["error"]
        assert vram.ensure_calls == 2 and len(pauses) == 1
        assert vram.running_calls == 0 and vram.stop_calls == 0

    def test_reprise_reussie_la_phase_poursuit(self, tmp_path, pauses, monkeypatch):
        cfg = _cloud_cfg(tmp_path)
        vram = _ScriptedVram([False, True])
        reached = []

        def _stage(*args, **kwargs):
            reached.append(True)
            raise RuntimeError("arrêt du test après la vérification LLM")

        monkeypatch.setattr(correction, "_prepare_and_stage_inputs", _stage)
        result = correction.run(_runner(cfg, vram), _job_with_srt(cfg), cfg)
        assert reached == [True]
        assert vram.ensure_calls == 2
        assert result == {"success": False, "error": "arrêt du test après la vérification LLM"}
        assert vram.stop_calls == 0  # rien n'a été lancé ici : aucun arrêt

    def test_local_inchange_echec_dur(self, tmp_path, pauses):
        cfg = _local_cfg(tmp_path)
        vram = _ScriptedVram([False])
        result = correction.run(_runner(cfg, vram), _job_with_srt(cfg), cfg)
        assert result == {"success": False, "error": "LLM d'arbitrage non disponible"}
        assert vram.ensure_calls == 1 and pauses == []

    def test_distant_non_cloud_garde_vram_wait(self, tmp_path, pauses):
        cfg = _local_cfg(tmp_path, arbitrage_llm_host="10.0.0.5")
        vram = _ScriptedVram([False])
        result = correction.run(_runner(cfg, vram), _job_with_srt(cfg), cfg)
        assert result.get("vram_wait") is True
        assert vram.ensure_calls == 1 and pauses == []


# ---------------------------------------------------------------- résumé
class TestResumePanneCloud:
    def test_summary_llm_failed_jamais_vram_wait(self, tmp_path, pauses):
        cfg = _cloud_cfg(tmp_path)
        vram = _ScriptedVram([False, False])
        result = {"transcript_text": "Bonjour à tous."}
        summary_llm.run_llm_summary(_runner(cfg, vram), make_job_stub(), result, cfg,
                                    logging.getLogger("test"))
        assert result.get("summary_llm_failed") is True
        assert "vram_wait" not in result
        assert vram.ensure_calls == 2 and len(pauses) == 1

    def test_local_inchange(self, tmp_path, pauses):
        cfg = _local_cfg(tmp_path)
        vram = _ScriptedVram([False])
        result = {"transcript_text": "Bonjour à tous."}
        summary_llm.run_llm_summary(_runner(cfg, vram), make_job_stub(), result, cfg,
                                    logging.getLogger("test"))
        assert result.get("summary_llm_failed") is True
        assert vram.ensure_calls == 1 and pauses == []


# ---------------------------------------------------------------- admission / reclaim / arrêt
class TestAdmissionEtArret:
    def _profile_with_llm(self):
        profile = get_profile(resolve_legacy_mode("quality"))
        assert profile.resource_requirements.needs_llm
        return profile

    def test_admission_cloud_sans_part_llm(self):
        res = pipeline_admission.estimate_profile_resources(_cloud_cfg(), self._profile_with_llm())
        assert "llm_arbitration" not in res["phases"]
        assert res["llm_shared"] is False

    def test_admission_locale_inchangee(self):
        res = pipeline_admission.estimate_profile_resources(_local_cfg(), self._profile_with_llm())
        assert res["phases"].get("llm_arbitration", 0) > 0

    def test_reclaim_sort_en_tete_en_cloud(self):
        vram = _ScriptedVram([], config=_cloud_cfg())
        allocator = FakeLlmLockAllocator()
        assert stop_idle_arbitrage_llm(allocator, vram) is False
        assert vram.running_calls == 0 and vram.stop_calls == 0
        assert allocator.acquire_calls == []

    def test_reclaim_local_inchange(self):
        vram = FakeArbitrageVram(arbitrage_running=True)
        vram.config = _local_cfg()
        assert stop_idle_arbitrage_llm(FakeLlmLockAllocator(), vram) is True
        assert vram.stop_calls == 1

    def test_release_fin_de_pipeline_noop_en_cloud(self):
        vram = _ScriptedVram([])
        allocator = FakeLlmLockAllocator()
        svc = SimpleNamespace(config=_cloud_cfg(), runner=SimpleNamespace(vram=vram, allocator=allocator),
                              _LLM_STOP_OWNER=PipelineService._LLM_STOP_OWNER)
        PipelineService._release_arbitrage_llm(svc)
        assert allocator.acquire_calls == []
        assert vram.running_calls == 0 and vram.stop_calls == 0

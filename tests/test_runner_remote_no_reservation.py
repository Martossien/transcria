"""Vérifie qu'une phase servie à distance ne réserve AUCUNE VRAM locale.

Bug corrigé (docs/SERVICE_RESSOURCES_GPU.md §9) : en mode distant, le runner
réservait quand même la VRAM des phases stt/diarization localement (`phase=stt
gpu=5 vram=6000` pendant un run 100 % distant), créant une fausse contention.
"""
from __future__ import annotations

from types import SimpleNamespace

from transcria.workflow.runner import WorkflowRunner, _NoReservationSession


class _RecordingAllocator:
    """Allocateur factice : enregistre les réservations demandées."""

    preferred_gpu = 0

    def __init__(self):
        self.reserve_calls: list[tuple] = []

    def try_reserve(self, job_id, required_mb, phase, preferred_gpu=None):
        self.reserve_calls.append((job_id, required_mb, phase))
        return SimpleNamespace(gpu_index=5)

    def get_gpu_info(self):
        return [{"id": 0}]

    def release_phase(self, *a, **k):
        pass


def _runner(config):
    r = WorkflowRunner(object, config)   # store non utilisé par les méthodes testées
    r.allocator = _RecordingAllocator()
    return r


_REMOTE_STT = {
    "models": {"stt_backend": "cohere"},
    "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
}
_REMOTE_DIAR = {"models": {"diarization_backend": "remote"}}
_LOCAL = {"models": {"stt_backend": "cohere", "diarization_backend": "pyannote"}}

_job = SimpleNamespace(id="job-1")


# ── Détection de phase distante ──────────────────────────────────────────────

def test_phase_runs_remotely_detection():
    assert _runner(_REMOTE_STT)._phase_runs_remotely("stt") is True
    assert _runner(_REMOTE_STT)._phase_runs_remotely("summary_stt") is True
    assert _runner(_REMOTE_DIAR)._phase_runs_remotely("diarization") is True
    r = _runner(_LOCAL)
    assert r._phase_runs_remotely("stt") is False
    assert r._phase_runs_remotely("diarization") is False
    assert r._phase_runs_remotely("speaker_detection") is False  # non couvert ici


# ── STT : pas de réservation en distant ──────────────────────────────────────

def test_stt_remote_reserves_nothing():
    r = _runner(_REMOTE_STT)
    reservation, managed = r._reserve_gpu_phase(_job, 6000, "stt")
    assert managed is False
    assert reservation.gpu_index == 0          # device de repli (preferred_gpu)
    assert r.allocator.reserve_calls == []      # AUCUNE réservation VRAM locale


def test_stt_local_still_reserves():
    r = _runner(_LOCAL)
    reservation, managed = r._reserve_gpu_phase(_job, 6000, "stt")
    assert managed is True
    assert r.allocator.reserve_calls == [("job-1", 6000, "stt")]


# ── Diarisation : session sans réservation en distant ────────────────────────

def test_diarization_remote_session_no_reservation():
    r = _runner(_REMOTE_DIAR)
    session = r._gpu_session(_job, "remote", 2000, "diarization")
    assert isinstance(session, _NoReservationSession)
    with session as gpu:
        assert gpu.gpu_index == 0
    assert r.allocator.reserve_calls == []


def test_diarization_local_uses_real_session():
    r = _runner(_LOCAL)
    session = r._gpu_session(_job, "pyannote", 2000, "diarization")
    assert not isinstance(session, _NoReservationSession)


def test_default_remote_gpu_index_falls_back_to_zero():
    r = _runner(_REMOTE_STT)
    r.allocator = SimpleNamespace()  # pas de preferred_gpu
    assert r._default_remote_gpu_index() == 0

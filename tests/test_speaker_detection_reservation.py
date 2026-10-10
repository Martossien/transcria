"""run_speaker_detection (passe lancée dès l'upload) réserve selon le backend RÉSOLU.

Avant 0.4.7 la phase réservait « pyannote » en dur, quel que soit le backend : avec
« auto » → Nemotron 3, cette passe tourne sur CPU sans toucher à la carte (c'est elle
que les utilisateurs voyaient attendre la VRAM), et la VRAM d'attente remontée est
celle du backend réellement retenu.
"""
from __future__ import annotations

from types import SimpleNamespace

from transcria.gpu.gpu_session import GPUSessionError
from transcria.workflow.phases import diarization as phase


class _Runner:
    def __init__(self, cuda=True, fail_session=False):
        self.sessions: list[tuple] = []
        self.detect_calls: list[str] = []
        self.states: list = []
        self._cuda = cuda
        self._fail = fail_session
        self.store = SimpleNamespace(update_state=lambda *a: self.states.append(a))
        self.vram = SimpleNamespace(pyannote_vram_mb=2000)

    def _cuda_available(self):
        return self._cuda

    def _pyannote_progress_callback(self, job, phase_name):
        return None

    def _gpu_session(self, job, model_name, required_mb, phase_name):
        self.sessions.append((model_name, required_mb, phase_name))
        if self._fail:
            raise GPUSessionError("VRAM insuffisante")

        class _S:
            gpu_index = 3

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return _S()

    def _detect_speakers(self, detector, job, audio_path, *, device, progress_callback):
        self.detect_calls.append(device)
        return {"available": True, "speakers": []}


def _job(hint=None):
    return SimpleNamespace(id="j1", get_extra_data=lambda: {"speaker_hint": hint} if hint else {})


def _nemotron_ready(tmp_path, monkeypatch):
    cli = tmp_path / "audiocpp_cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    (tmp_path / "m.gguf").write_bytes(b"x")
    monkeypatch.setenv("HF_TOKEN", "x")
    return {"cli_path": str(cli), "model_path": str(tmp_path / "m.gguf")}


def test_auto_nemotron_tourne_sur_cpu_sans_reservation(tmp_path, monkeypatch):
    cfg = {"models": {"diarization_backend": "auto"}, "nemotron_diar": _nemotron_ready(tmp_path, monkeypatch),
           "gpu": {"nemotron_diar_vram_mb": 2000}}
    r = _Runner()
    res = phase.run_speaker_detection(r, _job(), "a.wav", cfg, update_state=False)
    assert res["available"] is True
    assert r.sessions == []                     # aucune session GPU, aucune attente de VRAM
    assert r.detect_calls == ["cpu"]


def test_auto_au_dela_de_huit_reserve_pyannote(tmp_path, monkeypatch):
    cfg = {"models": {"diarization_backend": "auto"}, "nemotron_diar": _nemotron_ready(tmp_path, monkeypatch),
           "gpu": {"pyannote_vram_mb": 2500}}
    r = _Runner()
    phase.run_speaker_detection(r, _job({"min": 2, "max": 12}), "a.wav", cfg, update_state=False)
    assert r.sessions == [("pyannote", 2500, "speaker_detection")]
    assert r.detect_calls == ["cuda:3"]


def test_backend_explicite_reserve_sa_propre_vram():
    cfg = {"models": {"diarization_backend": "sortformer"}, "gpu": {"sortformer_vram_mb": 3500}}
    r = _Runner()
    phase.run_speaker_detection(r, _job(), "a.wav", cfg, update_state=True)
    assert r.sessions == [("sortformer", 3500, "speaker_detection")]
    assert [s[1].name for s in r.states] == ["SPEAKER_DETECTION_RUNNING", "SPEAKER_DETECTION_DONE"]


def test_vram_wait_remonte_la_vram_du_backend_resolu():
    cfg = {"models": {"diarization_backend": "pyannote"}, "gpu": {"pyannote_vram_mb": 2500}}
    r = _Runner(fail_session=True)
    res = phase.run_speaker_detection(r, _job(), "a.wav", cfg, update_state=False)
    assert res["vram_wait"] is True and res["required_mb"] == 2500 and res["phase"] == "speaker_detection"


def test_nemotron_cuda_force_reserve_le_gpu(tmp_path, monkeypatch):
    cfg = {"models": {"diarization_backend": "nemotron_diar"},
           "nemotron_diar": {**_nemotron_ready(tmp_path, monkeypatch), "backend": "cuda"},
           "gpu": {"nemotron_diar_vram_mb": 2000}}
    r = _Runner()
    phase.run_speaker_detection(r, _job(), "a.wav", cfg, update_state=False)
    assert r.sessions == [("nemotron_diar", 2000, "speaker_detection")]


def test_sans_cuda_cpu():
    cfg = {"models": {"diarization_backend": "pyannote"}}
    r = _Runner(cuda=False)
    phase.run_speaker_detection(r, _job(), "a.wav", cfg, update_state=False)
    assert r.sessions == [] and r.detect_calls == ["cpu"]

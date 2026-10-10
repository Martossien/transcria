"""Phase Résumé en mode « diariser d'abord » (workflow.summary_by_turns) : ordre et replis."""
from __future__ import annotations

from types import SimpleNamespace

from transcria.workflow.phases import summary as phase

_sl = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)


def _runner(result):
    calls = []

    def detect(job, audio_path, config, update_state=True):
        calls.append(("detect", update_state))
        return result
    return SimpleNamespace(run_speaker_detection=detect, calls=calls)


def test_detection_avant_stt_sans_toucher_l_etat():
    r = _runner({"available": True, "speakers": ["SPEAKER_00", "SPEAKER_01"]})
    out = phase.run_speaker_detection_before_transcription(r, SimpleNamespace(id="j"), "a.wav", {}, _sl)
    assert out["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert r.calls == [("detect", False)]            # jamais SPEAKER_DETECTION_RUNNING pendant le résumé


def test_vram_wait_ou_indisponible_ne_bloque_pas():
    for res in ({"vram_wait": True, "required_mb": 2000}, {"available": False, "message": "binaire absent"}):
        assert phase.run_speaker_detection_before_transcription(_runner(res), SimpleNamespace(id="j"), "a", {}, _sl) == {}


def test_exception_best_effort():
    def boom(*a, **k):
        raise RuntimeError("cuda")
    r = SimpleNamespace(run_speaker_detection=boom)
    assert phase.run_speaker_detection_before_transcription(r, SimpleNamespace(id="j"), "a", {}, _sl) == {}


def test_detection_desactivee():
    r = _runner({"available": True, "speakers": ["SPEAKER_00"]})
    cfg = {"workflow": {"enable_speaker_detection": False}}
    assert phase.run_speaker_detection_before_transcription(r, SimpleNamespace(id="j"), "a", cfg, _sl) == {}
    assert r.calls == []

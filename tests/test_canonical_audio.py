"""Étape WAV 16 kHz canonique (PISTES_AMELIORATION lot 2, §2.6) — opt-in, best-effort."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from builders import make_job_stub

from transcria.services.pipeline_steps import canonical_audio


class _Sl:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _svc(tmp_path, *, enabled):
    return SimpleNamespace(config={
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"audio_canonical_16k": {"enabled": enabled}},
    })


def test_desactive_par_defaut_passthrough(tmp_path, monkeypatch):
    def _no_call(src, dest):
        raise AssertionError("ffmpeg ne doit pas être appelé")

    monkeypatch.setattr(canonical_audio.AudioConverter, "convert_to_wav_mono_16k",
                        staticmethod(_no_call))
    out = canonical_audio.run(_svc(tmp_path, enabled=False), make_job_stub(),
                              str(tmp_path / "original.mp3"), _Sl())
    assert out == str(tmp_path / "original.mp3")


def test_active_convertit_et_rend_le_canonique(tmp_path, monkeypatch):
    calls = []

    def _fake_convert(src, dest):
        calls.append((Path(src), Path(dest)))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"RIFFWAVE16K")
        return True

    monkeypatch.setattr(canonical_audio.AudioConverter, "convert_to_wav_mono_16k",
                        staticmethod(_fake_convert))
    job = make_job_stub()
    out = canonical_audio.run(_svc(tmp_path, enabled=True), job,
                              str(tmp_path / "original.mp3"), _Sl())

    assert out.endswith("input/audio_16k.wav")
    assert job.id in out          # rangé dans le job, purgé avec lui
    assert len(calls) == 1


def test_reprise_sur_le_canonique_ne_reconvertit_pas(tmp_path, monkeypatch):
    def _no_call(src, dest):
        raise AssertionError("déjà canonique — pas de reconversion")

    monkeypatch.setattr(canonical_audio.AudioConverter, "convert_to_wav_mono_16k",
                        staticmethod(_no_call))
    path = str(tmp_path / "input" / "audio_16k.wav")
    out = canonical_audio.run(_svc(tmp_path, enabled=True), make_job_stub(), path, _Sl())
    assert out == path


def test_echec_ffmpeg_rend_l_original(tmp_path, monkeypatch):
    monkeypatch.setattr(canonical_audio.AudioConverter, "convert_to_wav_mono_16k",
                        staticmethod(lambda src, dest: False))
    original = str(tmp_path / "original.mp3")
    out = canonical_audio.run(_svc(tmp_path, enabled=True), make_job_stub(), original, _Sl())
    assert out == original

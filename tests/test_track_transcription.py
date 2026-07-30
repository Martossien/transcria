"""Mode PAR PISTE du transcripteur (vague 5, lot B) — intégration sans GPU.

Le scénario du gate réel (job 1ea75400 : 2 participants, 7,9 s de chevauchement) rejoué
en miniature : deux pistes alignées sur la timeline commune, un transcripteur factice, et
la preuve que LES MOTS DES DEUX locuteurs finissent dans les segments ET le SRT — là où
le mix n'aurait produit qu'un texte.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from transcria.stt.transcription import Transcriber

_SR = 16000


def _write_wav(path: Path, duration_s: float, freq: float) -> None:
    t = np.arange(int(duration_s * _SR)) / _SR
    samples = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(samples.tobytes())


class _FakeBackend:
    """Backend STT factice : « transcrit » chaque chunk en un segment reprenant son
    intervalle — suffisant pour prouver le câblage (fenêtres → chunks → fusion → SRT)."""

    model_name = "fake"
    concurrent_safe = False

    def transcribe(self, audio_path=None, language=None, audio_array=None,
                   sample_rate=None, **kw):
        n = len(audio_array) if audio_array is not None else 0
        return [{"start": 0.0, "end": round(n / _SR, 3),
                 "text": f"parole de {round(n / _SR, 1)}s"}]

    def segments_to_srt(self, segments, speaker_map=None):
        lines = []
        for i, s in enumerate(segments, 1):
            if not s.get("text"):
                continue
            lines.append(f"{i}\n{s['start']} --> {s['end']}\n"
                         f"{s.get('speaker', '?')}: {s['text']}\n")
        return "\n".join(lines)


@pytest.fixture
def job_with_tracks(tmp_path):
    """Un job minimal : manifeste v2 + 2 pistes alignées, chevauchement 2..3 s."""
    job_dir = tmp_path / "jobs" / "job-1"
    _write_wav(job_dir / "input" / "tracks" / "alice.wav", 3.0, 440)   # parle 0..3
    _write_wav(job_dir / "input" / "tracks" / "bob.wav", 5.0, 330)     # parle 2..5
    (job_dir / "metadata").mkdir(parents=True)
    (job_dir / "metadata" / "participants_manifest.json").write_text(json.dumps({
        "version": 2, "source": "jitsi", "mix": "timeline_common",
        "participants": [
            {"id": "alice", "name": "Alice", "kind": "unknown",
             "speech_windows": [[0.0, 3.0]], "track": "track_alice"},
            {"id": "bob", "name": "Bob", "kind": "unknown",
             "speech_windows": [[2.0, 5.0]], "track": "track_bob"},
        ]}))
    return tmp_path / "jobs"


def _transcriber(jobs_dir):
    cfg = {"storage": {"jobs_dir": str(jobs_dir)}, "models": {"stt_backend": "fake"},
           "workflow": {}}
    tr = Transcriber.__new__(Transcriber)                 # sans chargement de modèle réel
    tr.config = cfg
    tr.backend = "fake"
    tr.transcriber = _FakeBackend()
    tr.gpu_index = 0
    tr._last_chunk_metrics = None
    return tr


class _SL:
    def info(self, *a, **k): ...
    def warning(self, *a, **k): ...
    def set_context(self, **k): ...


class TestTranscribePerTrack:
    def test_les_mots_des_deux_locuteurs_existent(self, job_with_tracks):
        from transcria.jobs.filesystem import JobFilesystem

        tr = _transcriber(job_with_tracks)
        fs = JobFilesystem(str(job_with_tracks), "job-1")
        segments = tr._transcribe_per_track(fs, "fr", _SL())
        assert segments is not None
        speakers = [s["speaker"] for s in segments]
        assert speakers == ["Alice", "Bob"]               # tri global par début
        # Chevauchement 2..3 s CONSERVÉ : Alice finit APRÈS le début de Bob.
        assert segments[0]["end"] > segments[1]["start"]
        assert all(s["text"] for s in segments)           # chacun a SES mots

    def test_fenetres_pilotent_le_cout(self, job_with_tracks):
        """Bob n'est transcrit QUE sur sa fenêtre (2..5 s, +marge) — pas sur 0..5."""
        from transcria.jobs.filesystem import JobFilesystem

        tr = _transcriber(job_with_tracks)
        segments = tr._transcribe_per_track(
            JobFilesystem(str(job_with_tracks), "job-1"), "fr", _SL())
        bob = next(s for s in segments if s["speaker"] == "Bob")
        assert bob["start"] >= 1.5                        # 2.0 − marge 0.4, jamais 0
        assert bob["end"] <= 5.01

    def test_manifeste_v1_repli_mix(self, job_with_tracks):
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(str(job_with_tracks), "job-1")
        raw = json.loads((Path(str(job_with_tracks)) / "job-1" / "metadata" /
                          "participants_manifest.json").read_text())
        raw["version"] = 1
        for p in raw["participants"]:
            p.pop("track", None)
        fs.save_json("metadata/participants_manifest.json", raw)
        assert _transcriber(job_with_tracks)._transcribe_per_track(fs, "fr", _SL()) is None

    def test_piste_manquante_repli_mix(self, job_with_tracks):
        from transcria.jobs.filesystem import JobFilesystem

        (Path(str(job_with_tracks)) / "job-1" / "input" / "tracks" / "bob.wav").unlink()
        fs = JobFilesystem(str(job_with_tracks), "job-1")
        assert _transcriber(job_with_tracks)._transcribe_per_track(fs, "fr", _SL()) is None

    def test_sans_manifeste_repli_mix(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem

        (tmp_path / "jobs" / "job-2").mkdir(parents=True)
        fs = JobFilesystem(str(tmp_path / "jobs"), "job-2")
        assert _transcriber(tmp_path / "jobs")._transcribe_per_track(fs, "fr", _SL()) is None

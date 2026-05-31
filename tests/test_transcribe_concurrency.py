"""Tests de la concurrence du STT par tour (v1.1).

Vérifie : séquentiel par défaut / local ; parallèle réel quand le backend est
concurrent-safe + concurrency>1 ; et ORDRE des segments préservé dans tous les cas.
"""
from __future__ import annotations

import threading
import time

from transcria.stt.transcription import Transcriber


class _FakeTranscriber:
    """Encode l'id du tour dans audio_array[0] ; mesure le parallélisme réel."""

    def __init__(self, concurrent_safe: bool):
        self.concurrent_safe = concurrent_safe
        self.model_name = "fake-remote" if concurrent_safe else "fake-local"
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio_path=None, language="fr", audio_array=None, sample_rate=16000):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.02)
        with self._lock:
            self._active -= 1
        cid = int(audio_array[0])
        return [{"start": 0.0, "end": 1.0, "text": f"chunk-{cid}"}]


class _SL:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, *a, **k):
        if not a:
            return
        msg = str(a[0])
        if len(a) > 1:
            msg = msg % a[1:]
        self.messages.append(msg)


def _transcriber(fake, concurrency):
    t = Transcriber.__new__(Transcriber)          # sans create_transcriber
    t.config = {"inference": {"stt": {"concurrency": concurrency}}}
    t.transcriber = fake
    t.gpu_index = 0
    return t


def _chunks(n):
    return [{"audio": [float(i)], "start": i * 10.0, "speaker": "SPEAKER_00"} for i in range(n)]


def test_order_preserved_sequential():
    fake = _FakeTranscriber(concurrent_safe=False)
    t = _transcriber(fake, concurrency=4)          # ignoré : backend non concurrent-safe
    sl = _SL()
    segs = t._transcribe_by_chunks(_chunks(6), "fr", None, sl)
    assert [s["text"] for s in segs] == [f"chunk-{i}" for i in range(6)]
    assert [s["start"] for s in segs] == [i * 10.0 for i in range(6)]  # offsets globaux
    assert fake.max_active == 1                     # resté séquentiel
    assert any("Transcription par tour séquentielle: backend=fake-local" in msg for msg in sl.messages)
    assert any("Transcription par tour terminée: backend=fake-local workers=1 tours=6 segments=6" in msg for msg in sl.messages)
    assert t._last_chunk_metrics["mode"] == "sequential"
    assert t._last_chunk_metrics["workers"] == 1
    assert t._last_chunk_metrics["chunks"] == 6
    assert t._last_chunk_metrics["concurrent_safe"] is False


def test_concurrent_when_remote_and_configured():
    fake = _FakeTranscriber(concurrent_safe=True)
    t = _transcriber(fake, concurrency=4)
    sl = _SL()
    segs = t._transcribe_by_chunks(_chunks(8), "fr", None, sl)
    assert [s["text"] for s in segs] == [f"chunk-{i}" for i in range(8)]  # ORDRE préservé
    assert fake.max_active > 1                       # parallélisme réel observé
    assert any("Transcription par tour en concurrence: backend=fake-remote workers=4 tours=8" in msg for msg in sl.messages)
    assert any("Transcription par tour terminée: backend=fake-remote workers=4 tours=8 segments=8" in msg for msg in sl.messages)
    assert t._last_chunk_metrics["mode"] == "concurrent"
    assert t._last_chunk_metrics["workers"] == 4
    assert t._last_chunk_metrics["chunks"] == 8
    assert t._last_chunk_metrics["segments_per_s"] > 0


def test_concurrency_one_stays_sequential_even_if_remote():
    fake = _FakeTranscriber(concurrent_safe=True)
    t = _transcriber(fake, concurrency=1)
    t._transcribe_by_chunks(_chunks(5), "fr", None, _SL())
    assert fake.max_active == 1


def test_chunk_concurrency_resolution():
    remote = _transcriber(_FakeTranscriber(concurrent_safe=True), concurrency=4)
    local = _transcriber(_FakeTranscriber(concurrent_safe=False), concurrency=4)
    assert remote._chunk_concurrency(total=10) == 4
    assert remote._chunk_concurrency(total=2) == 2     # borné par le nb de tours
    assert remote._chunk_concurrency(total=1) == 1     # un seul tour → séquentiel
    assert local._chunk_concurrency(total=10) == 1     # backend non concurrent-safe


def test_default_concurrency_is_sequential():
    # Pas de clé concurrency → défaut 1.
    t = Transcriber.__new__(Transcriber)
    t.config = {"inference": {"stt": {}}}
    t.transcriber = _FakeTranscriber(concurrent_safe=True)
    t.gpu_index = 0
    assert t._chunk_concurrency(total=10) == 1


def test_invalid_concurrency_falls_back_to_sequential(caplog):
    t = _transcriber(_FakeTranscriber(concurrent_safe=True), concurrency="beaucoup")
    assert t._chunk_concurrency(total=10) == 1
    assert "inference.stt.concurrency invalide" in caplog.text


def test_zero_concurrency_falls_back_to_sequential(caplog):
    t = _transcriber(_FakeTranscriber(concurrent_safe=True), concurrency=0)
    assert t._chunk_concurrency(total=10) == 1
    assert "inference.stt.concurrency doit être >= 1" in caplog.text

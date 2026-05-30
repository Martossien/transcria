"""Tests de la concurrence du STT par tour (v1.1).

Vérifie : séquentiel par défaut / local ; parallèle réel quand le backend est
concurrent-safe + concurrency>1 ; et ORDRE des segments préservé dans tous les cas.
"""
from __future__ import annotations

import threading
import time

import pytest

from transcria.stt.transcription import Transcriber


class _FakeTranscriber:
    """Encode l'id du tour dans audio_array[0] ; mesure le parallélisme réel."""

    def __init__(self, concurrent_safe: bool):
        self.concurrent_safe = concurrent_safe
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
    def info(self, *a, **k):
        pass


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
    segs = t._transcribe_by_chunks(_chunks(6), "fr", None, _SL())
    assert [s["text"] for s in segs] == [f"chunk-{i}" for i in range(6)]
    assert [s["start"] for s in segs] == [i * 10.0 for i in range(6)]  # offsets globaux
    assert fake.max_active == 1                     # resté séquentiel


def test_concurrent_when_remote_and_configured():
    fake = _FakeTranscriber(concurrent_safe=True)
    t = _transcriber(fake, concurrency=4)
    segs = t._transcribe_by_chunks(_chunks(8), "fr", None, _SL())
    assert [s["text"] for s in segs] == [f"chunk-{i}" for i in range(8)]  # ORDRE préservé
    assert fake.max_active > 1                       # parallélisme réel observé


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

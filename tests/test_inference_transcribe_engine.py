"""Moteur STT du nœud de calcul : résidence, sérialisation, erreurs VRAM."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from inference_service.errors import GpuBusyError, UnprocessableError
from inference_service.transcribe_engine import TranscribeEngine


class _FakeBackend:
    """Transcripteur factice : compte ses chargements et ses appels."""

    instances = 0

    def __init__(self, segments=None, boom: Exception | None = None):
        _FakeBackend.instances += 1
        self._segments = segments if segments is not None else [{"start": 0.0, "end": 1.0,
                                                                 "text": "bonjour"}]
        self._boom = boom
        self.calls = 0
        self.offloaded = False

    def transcribe(self, path, language="fr"):
        self.calls += 1
        if self._boom:
            raise self._boom
        return self._segments

    def offload(self):
        self.offloaded = True


def _engine(**kw) -> tuple[TranscribeEngine, list]:
    created: list = []

    def factory(backend):
        obj = _FakeBackend(**kw)
        created.append(obj)
        return obj

    return TranscribeEngine({}, backend_factory=factory), created


def test_modele_charge_une_seule_fois():
    """Résidence : 3 appels ⇒ 1 seul chargement (sinon on rechargerait à chaque requête)."""
    engine, created = _engine()
    for _ in range(3):
        engine.transcribe(Path("/tmp/x.wav"))
    assert len(created) == 1 and created[0].calls == 3
    assert engine.loaded is True


def test_un_backend_resident_par_moteur_demande():
    engine, created = _engine()
    engine.transcribe(Path("/tmp/x.wav"), backend="cohere")
    engine.transcribe(Path("/tmp/x.wav"), backend="voxtral")
    engine.transcribe(Path("/tmp/x.wav"), backend="cohere")     # réutilise le premier
    assert len(created) == 2
    assert sorted(engine.status()["backends"]) == ["cohere", "voxtral"]


def test_appels_concurrents_serialises():
    """Deux requêtes simultanées ne doivent PAS calculer en même temps sur le GPU."""
    engine, _ = _engine()
    overlap = {"max": 0, "cur": 0}
    guard = threading.Lock()
    original = engine._ensure_loaded

    def slow_backend(backend):
        class _Slow(_FakeBackend):
            def transcribe(self, path, language="fr"):
                with guard:
                    overlap["cur"] += 1
                    overlap["max"] = max(overlap["max"], overlap["cur"])
                import time as _t
                _t.sleep(0.05)
                with guard:
                    overlap["cur"] -= 1
                return [{"start": 0.0, "end": 1.0, "text": "ok"}]
        return _Slow()

    engine._backend_factory = slow_backend
    del original
    threads = [threading.Thread(target=engine.transcribe, args=(Path("/tmp/x.wav"),))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap["max"] == 1                    # jamais deux calculs en parallèle


def test_vram_saturee_donne_une_erreur_explicite():
    engine, _ = _engine(boom=RuntimeError("CUDA out of memory"))
    with pytest.raises(GpuBusyError):
        engine.transcribe(Path("/tmp/x.wav"))


def test_autre_erreur_moteur_est_traduite():
    engine, _ = _engine(boom=ValueError("format inconnu"))
    with pytest.raises(UnprocessableError):
        engine.transcribe(Path("/tmp/x.wav"))


def test_dechargement_libere_les_backends():
    engine, created = _engine()
    engine.transcribe(Path("/tmp/x.wav"))
    assert engine.unload() is True
    assert created[0].offloaded is True and engine.loaded is False
    assert engine.unload() is False               # idempotent

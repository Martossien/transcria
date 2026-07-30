"""Sous-diarisation PAR PISTE (vague 5, lot B2) — sans GPU.

Ce que ces tests verrouillent : une piste salle où pyannote entend PLUSIEURS voix est
scindée en `PISTE_<pid>_S1`… dans `speaker_turns.json` (l'étape 5 les regroupe sous le
micro partagé) ; une seule voix, une piste solo, une piste quasi muette ou un backend
sans API par fichier → comportement historique, jamais un échec. Tout est journalisé
dans `speakers/track_diarization.json` (relisible : examinées, ignorées, pourquoi).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from transcria.jobs.filesystem import JobFilesystem
from transcria.stt.speaker_detection import SpeakerDetector


def _job_with_tracks(tmp_path, participants):
    job_dir = tmp_path / "jobs" / "job-1"
    (job_dir / "input" / "tracks").mkdir(parents=True)
    (job_dir / "metadata").mkdir(parents=True)
    for p in participants:
        if p.get("track"):
            (job_dir / "input" / "tracks" / (p["track"].removeprefix("track_") + ".wav")) \
                .write_bytes(b"fake-wav")
    (job_dir / "metadata" / "participants_manifest.json").write_text(json.dumps({
        "version": 2, "source": "jitsi", "mix": "timeline_common",
        "participants": participants}))
    fs = JobFilesystem(str(tmp_path / "jobs"), "job-1")
    # Clips déjà « présents » : detect() ne recrée pas un diarizeur pour les extraire.
    fs.save_json("speakers/speaker_clips.json", {})
    return fs


ROOM_P2 = {"id": "p2", "name": "Salle B", "kind": "unknown",
           "speech_windows": [[2.0, 20.0]], "track": "track_p2"}
SOLO_P1 = {"id": "p1", "name": "Alice", "kind": "solo",
           "speech_windows": [[0.0, 15.0]], "track": "track_p1"}


class _FakeDiarizer:
    """Diarizeur factice : rend un résultat pré-écrit par nom de fichier de piste."""

    def __init__(self, by_file):
        self.by_file = by_file
        self.calls: list[str] = []
        self.offloaded = False

    def diarize_audio(self, path):
        self.calls.append(path.name)
        return self.by_file[path.name]

    def offload(self):
        self.offloaded = True


def _two_voices(a=(2.0, 10.0), b=(10.0, 20.0)):
    turns = [{"start": a[0], "end": a[1], "speaker": "SPEAKER_00"},
             {"start": b[0], "end": b[1], "speaker": "SPEAKER_01"}]
    return {"available": True, "turns": turns, "exclusive_turns": turns,
            "speakers": ["SPEAKER_00", "SPEAKER_01"], "stats": {}}


def _one_voice():
    turns = [{"start": 2.0, "end": 20.0, "speaker": "SPEAKER_00"}]
    return {"available": True, "turns": turns, "exclusive_turns": turns,
            "speakers": ["SPEAKER_00"], "stats": {}}


def _detector(monkeypatch, tmp_path, fake):
    monkeypatch.setattr("transcria.stt.speaker_detection.create_diarizer",
                        lambda cfg, device=None, progress_callback=None: fake)
    return SpeakerDetector({"storage": {"jobs_dir": str(tmp_path / "jobs")}})


class TestSubdiarizeTracks:
    def test_deux_voix_scindent_la_piste(self, tmp_path, monkeypatch):
        fs = _job_with_tracks(tmp_path, [ROOM_P2])
        fake = _FakeDiarizer({"p2.wav": _two_voices()})
        det = _detector(monkeypatch, tmp_path, fake)

        result = det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        assert fake.calls == ["p2.wav"] and fake.offloaded
        turns = fs.load_json("speakers/speaker_turns.json")
        assert {"PISTE_p2_S1", "PISTE_p2_S2"} <= set(turns["speakers"])
        assert "Salle B" not in turns["speakers"]
        audit = fs.load_json("speakers/track_diarization.json")
        assert audit["tracks"]["p2"]["clusters"] == 2
        assert audit["tracks"]["p2"]["exclusive_turns"][0]["speaker"] == "PISTE_p2_S1"
        ids = [s["speaker_id"] for s in result["speakers"]]
        assert "PISTE_p2_S1" in ids                     # visibles à l'étape 5

    def test_une_voix_comportement_historique(self, tmp_path, monkeypatch):
        fs = _job_with_tracks(tmp_path, [ROOM_P2])
        det = _detector(monkeypatch, tmp_path, _FakeDiarizer({"p2.wav": _one_voice()}))

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        turns = fs.load_json("speakers/speaker_turns.json")
        assert turns["speakers"] == ["Salle B"]          # le nom reste proposé (cas fluide)
        audit = fs.load_json("speakers/track_diarization.json")
        assert audit["tracks"] == {} and "voix" in audit["skipped"]["p2"]

    def test_solo_et_quasi_muette_jamais_diarisees(self, tmp_path, monkeypatch):
        quiet = dict(ROOM_P2, id="p3", track="track_p3", speech_windows=[[0.0, 3.0]])
        _job_with_tracks(tmp_path, [SOLO_P1, quiet])
        fake = _FakeDiarizer({})                          # tout appel serait un KeyError
        det = _detector(monkeypatch, tmp_path, fake)

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        assert fake.calls == []                           # aucune passe pyannote
        audit = JobFilesystem(str(tmp_path / "jobs"), "job-1") \
            .load_json("speakers/track_diarization.json")
        assert audit["skipped"]["p1"] == "solo"
        assert "insuffisante" in audit["skipped"]["p3"]

    def test_backend_sans_api_fichier_reste_gracieux(self, tmp_path, monkeypatch):
        fs = _job_with_tracks(tmp_path, [ROOM_P2])
        det = _detector(monkeypatch, tmp_path, SimpleNamespace(offload=lambda: None))

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        assert fs.load_json("speakers/speaker_turns.json")["speakers"] == ["Salle B"]

    def test_echec_diarisation_une_piste_nabime_pas_les_autres(self, tmp_path, monkeypatch):
        broken = dict(ROOM_P2, id="p4", track="track_p4")
        fs = _job_with_tracks(tmp_path, [ROOM_P2, broken])
        det = _detector(monkeypatch, tmp_path, _FakeDiarizer({
            "p2.wav": _two_voices(),
            "p4.wav": {"available": False, "turns": [], "speakers": [], "error": "OOM"},
        }))

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        turns = fs.load_json("speakers/speaker_turns.json")
        assert "PISTE_p2_S1" in turns["speakers"]         # p2 scindée
        assert "Salle B" in turns["speakers"]             # p4 : repli mono-locuteur
        audit = fs.load_json("speakers/track_diarization.json")
        assert audit["skipped"]["p4"] == "OOM"

    def test_rejouable_speaker_turns_existant_jamais_recalcule(self, tmp_path, monkeypatch):
        fs = _job_with_tracks(tmp_path, [ROOM_P2])
        fake = _FakeDiarizer({"p2.wav": _two_voices()})
        det = _detector(monkeypatch, tmp_path, fake)
        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        assert fake.calls == ["p2.wav"]                   # une seule passe, pas deux
        assert fs.load_json("speakers/speaker_turns.json")["speakers"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

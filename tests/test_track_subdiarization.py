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

    def diarize_audio(self, path, *, speaker_params=None):
        self.calls.append(path.name)
        self.speaker_params = speaker_params
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


def _bleedy(dom=(0.0, 80.0), minor=(80.0, 92.0)):
    turns = [{"start": dom[0], "end": dom[1], "speaker": "SPEAKER_00"},
             {"start": minor[0], "end": minor[1], "speaker": "SPEAKER_01"}]
    return {"available": True, "turns": turns, "exclusive_turns": turns,
            "speakers": ["SPEAKER_00", "SPEAKER_01"], "stats": {}}


class TestRegleDominance:
    """Gate Zoom réel 2026-07-31 : la repisse du chevauchement fait un 2e cluster ~12 % —
    une piste NOMMÉE à voix dominante garde son NOM, la repisse est écartée du STT."""

    def test_voix_dominante_garde_le_nom_et_note_la_repisse(self, tmp_path, monkeypatch):
        # La minorité (80..92) coïncide avec la parole d'un AUTRE participant → repisse.
        other = {"id": "p9", "name": "Alice", "kind": "unknown",
                 "speech_windows": [[78.0, 95.0]], "track": "track_p9"}
        fs = _job_with_tracks(tmp_path, [ROOM_P2, other])
        det = _detector(monkeypatch, tmp_path, _FakeDiarizer(
            {"p2.wav": _bleedy(), "p9.wav": _one_voice()}))

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        turns = fs.load_json("speakers/speaker_turns.json")
        assert "Salle B" in turns["speakers"]             # le NOM survit (87 % dominant)
        assert "PISTE_p2_S2" not in turns["speakers"]
        audit = fs.load_json("speakers/track_diarization.json")
        assert audit["bleed"]["p2"] == [[80.0, 92.0]]     # la repisse, relisible
        assert "dominante" in audit["skipped"]["p2"]

    def test_partage_reel_reste_scinde(self, tmp_path, monkeypatch):
        fs = _job_with_tracks(tmp_path, [ROOM_P2])
        det = _detector(monkeypatch, tmp_path, _FakeDiarizer(
            {"p2.wav": _bleedy(dom=(0.0, 50.0), minor=(50.0, 92.0))}))   # 54 % : pas net

        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

        turns = fs.load_json("speakers/speaker_turns.json")
        assert {"PISTE_p2_S1", "PISTE_p2_S2"} <= set(turns["speakers"])

    def test_piste_sans_nom_toujours_scindee(self, tmp_path, monkeypatch):
        anon = dict(ROOM_P2, name="")
        fs = _job_with_tracks(tmp_path, [anon])
        det = _detector(monkeypatch, tmp_path, _FakeDiarizer({"p2.wav": _bleedy()}))
        det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")
        assert "PISTE_p2_S1" in fs.load_json("speakers/speaker_turns.json")["speakers"]


def test_la_fourchette_reunion_ne_force_jamais_une_piste(tmp_path, monkeypatch):
    """Cause racine du gate Zoom 2026-07-31 : hint {min:2,max:2} (RÉUNION) hérité par la
    diarisation PAR PISTE → chaque piste mono-voix scindée de force. La sous-diarisation
    passe speaker_params={} explicite : pyannote reste LIBRE sur chaque piste."""
    _job_with_tracks(tmp_path, [ROOM_P2])
    fake = _FakeDiarizer({"p2.wav": _one_voice()})
    monkeypatch.setattr("transcria.stt.speaker_detection.create_diarizer",
                        lambda cfg, device=None, progress_callback=None: fake)
    det = SpeakerDetector({"storage": {"jobs_dir": str(tmp_path / "jobs")},
                           "diarization": {"min_speakers": 2, "max_speakers": 2}})
    det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")
    assert fake.speaker_params == {}                     # jamais la fourchette réunion


def test_personne_discrete_jamais_prise_pour_de_la_repisse(tmp_path, monkeypatch):
    """Le discriminateur : la REPISSE ne sonne que quand un AUTRE participant parle ; une
    vraie personne discrète (15 % du temps) parle aussi dans leurs SILENCES → la piste
    est scindée, ses mots ne sont jamais jetés."""
    other = {"id": "p9", "name": "Alice", "kind": "unknown",
             "speech_windows": [[0.0, 10.0]], "track": "track_p9"}   # Alice parle 0..10
    room = dict(ROOM_P2, speech_windows=[[2.0, 100.0]])
    _job_with_tracks(tmp_path, [room, other])
    # Voix minoritaire de p2 à 80..92 : PERSONNE d'autre ne parle à ce moment-là.
    fake = _FakeDiarizer({"p2.wav": _bleedy(dom=(2.0, 80.0), minor=(80.0, 92.0)),
                          "p9.wav": _one_voice()})
    monkeypatch.setattr("transcria.stt.speaker_detection.create_diarizer",
                        lambda cfg, device=None, progress_callback=None: fake)
    det = SpeakerDetector({"storage": {"jobs_dir": str(tmp_path / "jobs")}})
    fs = JobFilesystem(str(tmp_path / "jobs"), "job-1")

    det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

    turns = fs.load_json("speakers/speaker_turns.json")
    assert {"PISTE_p2_S1", "PISTE_p2_S2"} <= set(turns["speakers"])  # scindée, rien de perdu


def test_repisse_confirmee_par_la_parole_des_autres(tmp_path, monkeypatch):
    """Inverse : la voix minoritaire coïncide avec la parole d'un autre participant →
    repisse confirmée, le nom survit."""
    other = {"id": "p9", "name": "Alice", "kind": "unknown",
             "speech_windows": [[78.0, 95.0]], "track": "track_p9"}  # Alice parle 78..95
    room = dict(ROOM_P2, speech_windows=[[2.0, 100.0]])
    _job_with_tracks(tmp_path, [room, other])
    fake = _FakeDiarizer({"p2.wav": _bleedy(dom=(2.0, 80.0), minor=(80.0, 92.0)),
                          "p9.wav": _one_voice()})
    monkeypatch.setattr("transcria.stt.speaker_detection.create_diarizer",
                        lambda cfg, device=None, progress_callback=None: fake)
    det = SpeakerDetector({"storage": {"jobs_dir": str(tmp_path / "jobs")}})
    fs = JobFilesystem(str(tmp_path / "jobs"), "job-1")

    det.detect(SimpleNamespace(id="job-1"), tmp_path / "mix.wav")

    turns = fs.load_json("speakers/speaker_turns.json")
    assert "Salle B" in turns["speakers"]                # le nom survit
    assert "PISTE_p2_S2" not in turns["speakers"]

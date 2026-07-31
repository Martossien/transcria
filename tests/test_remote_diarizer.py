"""RemoteDiarizer : transmission de la fourchette de locuteurs par job au nœud distant."""

from types import SimpleNamespace

from transcria.stt.remote_diarizer import RemoteDiarizer

_CANONICAL = {"available": True, "turns": [], "exclusive_turns": [], "speakers": [], "stats": {}}


class _CaptureClient:
    def __init__(self):
        self.speaker_params = "UNSET"

    def diarize(self, audio_path, speaker_params=None):
        self.speaker_params = speaker_params
        return dict(_CANONICAL)


def _diarizer(tmp_path, diar_cfg, client):
    cfg = {
        "storage": {"jobs_dir": str(tmp_path)},
        "models": {},
        "inference": {},
        "diarization": {"cache_enabled": False, **diar_cfg},
    }
    return RemoteDiarizer(cfg, client=client)


def _run(tmp_path, diar_cfg):
    client = _CaptureClient()
    diar = _diarizer(tmp_path, diar_cfg, client)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"fake audio")
    diar.diarize(SimpleNamespace(id="job-remote-1"), audio)
    return client


def test_forwards_min_max_speaker_hint(tmp_path):
    client = _run(tmp_path, {"min_speakers": 3, "max_speakers": 7})
    assert client.speaker_params == {"min_speakers": 3, "max_speakers": 7}


def test_forwards_exact_num_speakers(tmp_path):
    client = _run(tmp_path, {"num_speakers": 5, "min_speakers": 5, "max_speakers": 5})
    assert client.speaker_params == {"num_speakers": 5, "min_speakers": 5, "max_speakers": 5}


def test_no_hint_forwards_none(tmp_path):
    client = _run(tmp_path, {})
    assert client.speaker_params is None


class TestDiarizeAudioParite:
    """Lot B2 × mode SPLIT : la sous-diarisation par piste doit fonctionner quand la
    diarisation est DISTANTE — les modes sont équivalents (règle de la maison)."""

    def test_fichier_part_au_noeud_et_dict_canonique(self):
        calls = {}

        class _Client:
            def diarize(self, path, speaker_params=None):
                calls["path"] = path.name
                calls["params"] = speaker_params
                return {"available": True, "turns": [{"start": 0.0, "end": 2.0,
                        "speaker": "SPEAKER_00"}], "exclusive_turns": [],
                        "speakers": ["SPEAKER_00"], "stats": {}}

        from pathlib import Path

        from transcria.stt.remote_diarizer import RemoteDiarizer
        diar = RemoteDiarizer({"inference": {}, "models": {}}, client=_Client())
        result = diar.diarize_audio(Path("/tmp/p2.wav"))
        assert result["available"] and result["speakers"] == ["SPEAKER_00"]
        assert calls["path"] == "p2.wav"

    def test_indisponibilite_best_effort_jamais_une_levee(self):
        from pathlib import Path

        from transcria.inference.client import InferenceUnavailable
        from transcria.stt.remote_diarizer import RemoteDiarizer

        class _Down:
            def diarize(self, path, speaker_params=None):
                raise InferenceUnavailable("nœud injoignable")

        diar = RemoteDiarizer({"inference": {}, "models": {}}, client=_Down())
        result = diar.diarize_audio(Path("/tmp/p2.wav"))
        assert result["available"] is False and "injoignable" in result["error"]

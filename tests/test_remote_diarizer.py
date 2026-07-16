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

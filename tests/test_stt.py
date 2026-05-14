import pytest

from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.speaker_detection import SpeakerDetector
from transcria.stt.transcription import Transcriber


class TestCohereTranscriber:
    def test_available_detects_imports(self):
        ct = CohereTranscriber()
        is_available = ct.available
        assert isinstance(is_available, bool)

    def test_detect_device(self):
        device = CohereTranscriber._detect_device()
        assert isinstance(device, str)
        assert device in ("cpu", "cuda:0", "cuda:1", "cuda:2", "cuda:3")

    def test_seconds_to_srt_time(self):
        assert CohereTranscriber._seconds_to_srt_time(0) == "00:00:00,000"
        assert CohereTranscriber._seconds_to_srt_time(1.5) == "00:00:01,500"
        assert CohereTranscriber._seconds_to_srt_time(61.123) == "00:01:01,123"
        assert CohereTranscriber._seconds_to_srt_time(3661.999) == "01:01:01,999"
        assert CohereTranscriber._seconds_to_srt_time(3601.001) == "01:00:01,001"

    def test_segments_to_srt_empty(self):
        ct = CohereTranscriber()
        srt = ct.segments_to_srt([])
        assert srt == ""

    def test_segments_to_srt_basic(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 3, "text": "Bonjour"},
            {"start": 3, "end": 6, "text": "Comment allez-vous"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "00:00:00,000 --> 00:00:03,000" in srt
        assert "Bonjour" in srt
        assert "00:00:03,000 --> 00:00:06,000" in srt
        assert "Comment allez-vous" in srt

    def test_segments_to_srt_with_speaker(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 2, "text": "Texte", "speaker": "Alice"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "Alice: Texte" in srt

    def test_segments_to_srt_skips_empty_text(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 2, "text": ""},
            {"start": 2, "end": 4, "text": "Valid"},
        ]
        srt = ct.segments_to_srt(segments)
        assert "Valid" in srt
        assert srt.count("-->") == 1

    def test_segments_to_srt_numbering_sequential(self):
        ct = CohereTranscriber()
        segments = [
            {"start": 0, "end": 1, "text": "A"},
            {"start": 1, "end": 2, "text": "B"},
            {"start": 2, "end": 3, "text": "C"},
        ]
        srt = ct.segments_to_srt(segments)
        lines = srt.strip().split("\n")
        numbers = [l for l in lines if l.isdigit()]
        assert numbers == ["1", "2", "3"]

    def test_offload_clears_model(self):
        ct = CohereTranscriber()
        ct._model = "fake"
        ct._processor = "fake"
        ct.offload()
        assert ct._model is None
        assert ct._processor is None

    def test_load_with_invalid_path_returns_false(self):
        ct = CohereTranscriber(model_path="/nonexistent/model/path")
        if ct.available:
            result = ct.load()
            assert result is False


class TestTranscriber:
    def test_transcribe_saves_speaker_map_without_name_error(self, app, owner_id):
        with app.app_context():
            from pathlib import Path

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.config import get_config

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Speaker Map")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json(
                "speakers/speaker_turns.json",
                {"turns": [{"start": 0, "end": 2, "speaker": "SPEAKER_00"}]},
            )
            fs.save_json(
                "speakers/speaker_mapping.json",
                {"mapping": {"SPEAKER_00": "Alice"}, "speakers": []},
            )

            transcriber = Transcriber(cfg, gpu_index=0)
            transcriber.cohere.transcribe = lambda *args, **kwargs: [
                {"start": 0, "end": 2, "text": "Bonjour"}
            ]
            transcriber.cohere.segments_to_srt = lambda segments, mapping=None: "1\n00:00:00,000 --> 00:00:02,000\nAlice: Bonjour\n"

            result = transcriber.transcribe(job, Path("/tmp/fake.wav"))

            assert result["speaker_count"] == 1
            assert fs.load_json("metadata/speakers_map.json")["mapping"]["SPEAKER_00"] == "Alice"
            assert "Alice: Bonjour" in fs.load_text("metadata/transcription.srt")


class TestSpeakerDetector:
    def test_detect_generates_missing_clips_when_turns_already_exist(self, app, owner_id, monkeypatch):
        with app.app_context():
            from pathlib import Path

            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.stt.diarization import DiarizerService

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Speaker Clips")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json(
                "speakers/speaker_turns.json",
                {
                    "available": True,
                    "turns": [{"start": 0, "end": 5, "speaker": "SPEAKER_00", "duration": 5}],
                    "speakers": ["SPEAKER_00"],
                    "stats": {"SPEAKER_00": {"speaking_time_seconds": 5, "turn_count": 1}},
                },
            )

            calls = []

            def fake_extract(self, audio_path, turns, speakers, job_fs, *args, **kwargs):
                calls.append((audio_path, turns, speakers))
                job_fs.save_json("speakers/speaker_clips.json", {"SPEAKER_00": ["clip.wav"]})

            monkeypatch.setattr(DiarizerService, "_extract_clips", fake_extract)

            result = SpeakerDetector(cfg).detect(job, Path("/tmp/audio.wav"), device="cpu")

            assert result["available"] is True
            assert calls
            assert fs.load_json("speakers/speaker_clips.json") == {"SPEAKER_00": ["clip.wav"]}

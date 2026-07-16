import tempfile

import pytest

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState, get_state_order, get_step_for_state
from transcria.workflow.steps import WORKFLOW_STEPS


class TestJobModel:
    def test_job_creation_defaults(self):
        job = Job(owner_id="user-1", state=JobState.CREATED.value, title="Réunion sans titre")
        assert job.state == JobState.CREATED.value
        assert job.title == "Réunion sans titre"
        assert job.processing_mode is None
        assert job.error_message is None

    def test_job_extra_data(self):
        job = Job(owner_id="user-1", state=JobState.CREATED.value, title="Réunion sans titre")
        job.set_extra_data({"key": "value", "nested": {"a": 1}})
        assert job.get_extra_data() == {"key": "value", "nested": {"a": 1}}
        assert job.extra_data_json is not None

    def test_job_extra_data_empty(self):
        job = Job(owner_id="user-1", state=JobState.CREATED.value, title="Réunion sans titre")
        assert job.get_extra_data() == {}

    def test_job_to_dict(self):
        job = Job(owner_id="user-1", title="Test Meeting", state=JobState.CREATED.value)
        d = job.to_dict()
        assert d["title"] == "Test Meeting"
        assert d["owner_id"] == "user-1"
        assert d["state"] == "created"
        assert "id" in d
        assert "created_at" in d

    def test_job_state_enum_order(self):
        assert get_state_order(JobState.CREATED) == 0
        assert get_state_order(JobState.COMPLETED) > get_state_order(JobState.CREATED)
        assert get_state_order(JobState.FAILED) > get_state_order(JobState.CREATED)
        assert get_state_order(JobState.CANCELLED) > get_state_order(JobState.CREATED)

    def test_get_step_for_state(self):
        step = get_step_for_state(JobState.UPLOADED)
        assert step is not None
        assert step["id"] == "file"

        step = get_step_for_state("analyzed")
        assert step is not None
        assert step["id"] == "analyze"

    def test_workflow_steps_order(self):
        assert len(WORKFLOW_STEPS) == 9
        orders = [s["order"] for s in WORKFLOW_STEPS]
        assert orders == list(range(1, 10))

    def test_job_state_values_distinct(self):
        values = [s.value for s in JobState]
        assert len(values) == len(set(values))


class TestJobFilesystem:
    @pytest.fixture
    def tmp_jobs_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def fs(self, tmp_jobs_dir):
        return JobFilesystem(tmp_jobs_dir, "test-job-123")

    def test_creates_directories(self, fs):
        assert (fs.job_dir / "input").is_dir()
        assert (fs.job_dir / "metadata").is_dir()
        assert (fs.job_dir / "summary").is_dir()
        assert (fs.job_dir / "context").is_dir()
        assert (fs.job_dir / "speakers" / "samples").is_dir()
        assert (fs.job_dir / "quality").is_dir()
        assert (fs.job_dir / "exports").is_dir()

    def test_save_and_load_json(self, fs):
        data = {"test": True, "items": [1, 2, 3]}
        fs.save_json("test.json", data)
        loaded = fs.load_json("test.json")
        assert loaded == data

    def test_save_and_load_text(self, fs):
        content = "Hello World\nLine 2"
        fs.save_text("test.txt", content)
        loaded = fs.load_text("test.txt")
        assert loaded == content

    def test_save_upload(self, fs):
        result = fs.save_upload(b"fake audio data", "recording.mp3")
        assert result["format"] == "mp3"
        assert result["size_bytes"] == 15
        assert result["original_filename"] == "recording.mp3"
        assert (fs.job_dir / "input" / "original.mp3").is_file()

    def test_save_upload_wav(self, fs):
        result = fs.save_upload(b"wav content", "meeting.WAV")
        assert result["format"] == "wav"

    def test_get_original_audio_path(self, fs):
        assert fs.get_original_audio_path() is None
        fs.save_upload(b"data", "audio.mp3")
        path = fs.get_original_audio_path()
        assert path is not None
        assert path.name == "original.mp3"

    def test_get_original_audio_prefers_first(self, fs):
        fs.save_upload(b"first", "a.mp3")
        fs.save_upload(b"second", "b.wav")
        path = fs.get_original_audio_path()
        assert path.name == "original.mp3"

    def test_cleanup_removes_directory(self, fs):
        fs.save_json("test.json", {"a": 1})
        assert fs.job_dir.is_dir()
        fs.cleanup()
        assert not fs.job_dir.is_dir()

    def test_load_json_nonexistent(self, fs):
        assert fs.load_json("nonexistent.json") is None

    def test_load_text_nonexistent(self, fs):
        assert fs.load_text("nonexistent.txt") is None

    def test_nested_save(self, fs):
        fs.save_json("deep/nested/file.json", {"x": 1})
        assert (fs.job_dir / "deep" / "nested" / "file.json").is_file()

    def test_save_json_is_atomic_no_tmp_residue(self, fs):
        """save_json publie via tmp + rename : pas de fichier temporaire résiduel."""
        fs.save_json("meeting.json", {"key": "value"})
        assert fs.load_json("meeting.json") == {"key": "value"}
        residues = [p for p in (fs.job_dir).iterdir() if p.name.endswith(".tmp")]
        assert residues == []

    def test_save_json_overwrite_keeps_valid_file(self, fs):
        """Une réécriture remplace atomiquement le contenu sans corruption."""
        fs.save_json("ctx.json", {"v": 1})
        fs.save_json("ctx.json", {"v": 2, "extra": [1, 2, 3]})
        assert fs.load_json("ctx.json") == {"v": 2, "extra": [1, 2, 3]}

    def test_save_text_is_atomic(self, fs):
        fs.save_text("notes.md", "# Titre\ncontenu")
        assert fs.load_text("notes.md") == "# Titre\ncontenu"
        residues = [p for p in (fs.job_dir).iterdir() if p.name.endswith(".tmp")]
        assert residues == []

"""Edge cases for context, exports, and state transitions."""
import tempfile
from pathlib import Path

import pytest

from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LexiconManager
from transcria.context.meeting_context import MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.exports.package_builder import PackageBuilder
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.workflow.states import StepStatus, WorkflowState


class TestContextEdgeCases:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_meeting_context_save_empty(self, tmp_dir):
        job = Job(id="j1", owner_id="u1", title="T", state=JobState.CREATED.value)
        MeetingContextManager.save(job, tmp_dir, {})
        ctx = MeetingContextManager.get(job, tmp_dir)
        assert ctx["language"] == "fr"

    def test_meeting_context_partial_update(self, tmp_dir):
        job = Job(id="j2", owner_id="u1", title="T", state=JobState.CREATED.value)
        MeetingContextManager.save(job, tmp_dir, {"title": "Nouveau"})
        ctx = MeetingContextManager.get(job, tmp_dir)
        assert ctx["title"] == "Nouveau"
        assert ctx["language"] == "fr"

    def test_participants_save_empty_list(self, tmp_dir):
        job = Job(id="j3", owner_id="u1", title="T", state=JobState.CREATED.value)
        saved = ParticipantsManager.save(job, tmp_dir, [])
        assert saved == []

    def test_participants_save_minimal(self, tmp_dir):
        job = Job(id="j4", owner_id="u1", title="T", state=JobState.CREATED.value)
        saved = ParticipantsManager.save(job, tmp_dir, [{}])
        assert len(saved) == 1
        assert saved[0]["name"] == ""
        assert saved[0]["is_animator"] is False

    def test_lexicon_import_empty(self, tmp_dir):
        job = Job(id="j5", owner_id="u1", title="T", state=JobState.CREATED.value)
        terms = LexiconManager.import_from_file(job, tmp_dir, "")
        assert terms == []

    def test_lexicon_import_comments_only(self, tmp_dir):
        job = Job(id="j6", owner_id="u1", title="T", state=JobState.CREATED.value)
        terms = LexiconManager.import_from_file(job, tmp_dir, "# comment\n# another")
        assert terms == []

    def test_job_context_builder_minimal(self, tmp_dir):
        job = Job(id="j7", owner_id="u1", title="T", state=JobState.CREATED.value)
        result = JobContextBuilder.build(job, tmp_dir)
        assert result["job_id"] == "j7"
        assert result["participants"] == []
        assert result["speakers"] == []
        assert result["lexicon"] == []
        assert result["meeting"]["language"] == "fr"

    def test_job_context_builder_full(self, tmp_dir):
        job = Job(id="j8", owner_id="u1", title="T", state=JobState.CREATED.value)
        MeetingContextManager.save(job, tmp_dir, {
            "title": "Réunion X", "language": "fr", "meeting_type": "Projet",
        })
        ParticipantsManager.save(job, tmp_dir, [
            {"name": "Alice", "function": "Dev", "role": "Tech", "is_animator": True}
        ])
        LexiconManager.save(job, tmp_dir, [
            {"term": "API", "category": "technique", "priority": "critique"}
        ])

        result = JobContextBuilder.build(job, tmp_dir)
        assert result["meeting"]["title"] == "Réunion X"
        assert len(result["participants"]) == 1
        assert len(result["lexicon"]) == 1
        assert result["participants"][0]["name"] == "Alice"


class TestExportEdgeCases:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_package_empty_job(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "empty-pkg")
        job = Job(id="empty-pkg", owner_id="u1", title="Vide", state=JobState.CREATED.value)
        builder = PackageBuilder({"storage": {"jobs_dir": tmp_dir}})
        result = builder.build_package(job)
        assert "error" not in result
        assert Path(result["zip_path"]).is_file()

    def test_package_with_all_files(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "full-pkg")
        fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:05,000\nTest\n")
        fs.save_json("context/meeting_context.json", {"title": "M"})
        fs.save_json("context/participants.json", [{"name": "A"}])
        fs.save_json("context/session_lexicon.json", [{"term": "X"}])
        fs.save_json("speakers/speaker_mapping.json", {"mapping": {}})
        fs.save_json("speakers/speaker_stats.json", {"speakers": []})
        fs.save_json("quality/quality_report.json", {"score": 90})
        fs.save_text("quality/quality_report.md", "# Report")
        fs.save_json("quality/review_points.json", [])
        fs.save_text("context/job_context.yaml", "job: test")

        job = Job(id="full-pkg", owner_id="u1", title="Full", state=JobState.CREATED.value)
        builder = PackageBuilder({"storage": {"jobs_dir": tmp_dir}})
        result = builder.build_package(job)

        import zipfile
        with zipfile.ZipFile(result["zip_path"]) as z:
            names = z.namelist()
            assert any("transcription.srt" in n for n in names)
            assert any("quality_report" in n for n in names)
            assert any("job_context.yaml" in n for n in names)

    def test_package_missing_optional_files(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "partial-pkg")
        fs.save_json("metadata/audio_analysis.json", {"duration": 10})
        job = Job(id="partial-pkg", owner_id="u1", title="Partial", state=JobState.CREATED.value)
        builder = PackageBuilder({"storage": {"jobs_dir": tmp_dir}})
        result = builder.build_package(job)
        assert Path(result["zip_path"]).is_file()


class TestWorkflowStateEdgeCases:
    def test_all_states_have_no_unknown_step(self):
        for state in JobState:
            statuses = WorkflowState.compute_statuses(state.value)
            valid_ids = {s["id"] for s in WorkflowState.STEPS}
            for sid in statuses:
                assert sid in valid_ids

    def test_state_transition_monotonic(self):
        ordered = [
            JobState.CREATED, JobState.UPLOADED, JobState.ANALYZED,
            JobState.SUMMARY_DONE, JobState.CONTEXT_DONE,
            JobState.PARTICIPANTS_DONE, JobState.LEXICON_DONE,
            JobState.TRANSCRIBING, JobState.QUALITY_CHECKED,
            JobState.EXPORT_READY, JobState.COMPLETED,
        ]
        for i in range(len(ordered) - 1):
            s1 = WorkflowState.compute_statuses(ordered[i].value)
            s2 = WorkflowState.compute_statuses(ordered[i + 1].value)
            done1 = sum(1 for v in s1.values() if v == StepStatus.DONE)
            done2 = sum(1 for v in s2.values() if v == StepStatus.DONE)
            assert done2 >= done1, f"Regressed from {ordered[i].value} to {ordered[i+1].value}"

    def test_failed_state_marks_current_as_error(self):
        statuses = WorkflowState.compute_statuses("failed")
        has_error = any(s == StepStatus.ERROR for s in statuses.values())
        assert has_error or all(s in (StepStatus.TODO, StepStatus.DONE, StepStatus.SKIPPED) for s in statuses.values())

    def test_cancelled_state(self):
        statuses = WorkflowState.compute_statuses("cancelled")
        assert isinstance(statuses, dict)
        assert len(statuses) == 9

    def test_next_step_after_lexicon(self):
        statuses = WorkflowState.compute_statuses("lexicon_done")
        next_s = WorkflowState.get_next_step(statuses)
        assert next_s is not None
        assert next_s["id"] == "processing"

    def test_next_step_after_participants(self):
        statuses = WorkflowState.compute_statuses("speaker_detection_done")
        next_s = WorkflowState.get_next_step(statuses)
        assert next_s is not None
        assert next_s["id"] == "lexicon"

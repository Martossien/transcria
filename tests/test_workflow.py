import pytest

from transcria.workflow.states import WorkflowState, StepStatus
from transcria.workflow.steps import WorkflowSteps
from transcria.jobs.models import JobState
from transcria.workflow.runner import WorkflowRunner


class TestWorkflowState:
    def test_get_steps_returns_10(self):
        steps = WorkflowState.get_steps()
        assert len(steps) == 9

    def test_steps_have_required_fields(self):
        for step in WorkflowState.STEPS:
            assert "id" in step
            assert "label" in step
            assert "order" in step
            assert "route" in step

    def test_steps_ordered_1_to_10(self):
        steps = WorkflowState.STEPS
        for i, step in enumerate(steps, 1):
            assert step["order"] == i

    def test_compute_statuses_created(self):
        statuses = WorkflowState.compute_statuses("created")
        assert statuses["file"] == StepStatus.TODO
        assert statuses["analyze"] == StepStatus.TODO
        assert statuses["export"] == StepStatus.TODO

    def test_compute_statuses_uploaded(self):
        statuses = WorkflowState.compute_statuses("uploaded")
        assert statuses["file"] == StepStatus.DONE
        assert statuses["analyze"] == StepStatus.IN_PROGRESS

    def test_compute_statuses_analyzed(self):
        statuses = WorkflowState.compute_statuses("analyzed")
        assert statuses["file"] == StepStatus.DONE
        assert statuses["analyze"] == StepStatus.DONE
        assert statuses["summary"] == StepStatus.IN_PROGRESS

    def test_compute_statuses_summary_done(self):
        statuses = WorkflowState.compute_statuses("summary_done")
        assert statuses["summary"] == StepStatus.DONE
        assert statuses["context"] == StepStatus.IN_PROGRESS

    def test_compute_statuses_completed(self):
        statuses = WorkflowState.compute_statuses("completed")
        for step_id in statuses:
            assert statuses[step_id] == StepStatus.DONE

    def test_compute_statuses_failed(self):
        statuses = WorkflowState.compute_statuses("failed")
        for step_id, st in statuses.items():
            assert st in (StepStatus.TODO, StepStatus.DONE, StepStatus.ERROR, StepStatus.SKIPPED)

    def test_compute_statuses_cancelled(self):
        statuses = WorkflowState.compute_statuses("cancelled")
        assert any(st == StepStatus.SKIPPED for st in statuses.values()) or all(
            st in (StepStatus.TODO, StepStatus.DONE) for st in statuses.values()
        )

    def test_get_next_step_created(self):
        statuses = WorkflowState.compute_statuses("created")
        next_s = WorkflowState.get_next_step(statuses)
        assert next_s["id"] == "file"

    def test_get_next_step_uploaded(self):
        statuses = WorkflowState.compute_statuses("uploaded")
        next_s = WorkflowState.get_next_step(statuses)
        assert next_s["id"] == "analyze"

    def test_get_next_step_completed(self):
        statuses = WorkflowState.compute_statuses("completed")
        next_s = WorkflowState.get_next_step(statuses)
        assert next_s is None

    def test_all_steps_mapped_to_statuses(self):
        valid_ids = {s["id"] for s in WorkflowState.STEPS}
        for state in JobState:
            statuses = WorkflowState.compute_statuses(state.value)
            for sid in statuses:
                assert sid in valid_ids, f"Unknown step id {sid} for state {state.value}"


class TestWorkflowRunner:
    def test_write_diarization_context_for_summary_llm(self, app, owner_id):
        with app.app_context():
            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Diar Context")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

            content = WorkflowRunner._write_diarization_context(
                fs,
                {
                    "speakers": [
                        {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 90, "turn_count": 9},
                        {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 30, "turn_count": 3},
                    ]
                },
            )

            saved = fs.load_text("summary/diarization_context.md")
            assert content == saved
            assert "Nombre de locuteurs détectés :** 2" in saved
            assert "SPEAKER_00" in saved
            assert "75.0%" in saved

    def test_run_summary_marks_failed_on_exception(self, app, owner_id, monkeypatch):
        with app.app_context():
            from transcria.config import get_config
            from transcria.jobs.store import JobStore
            from transcria.stt.summary import SummaryGenerator

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Summary Failure")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(runner.vram, "stop_qwen_35b", lambda: True)

            def raise_summary(*args, **kwargs):
                raise RuntimeError("boom")

            monkeypatch.setattr(SummaryGenerator, "generate_quick_summary", raise_summary)

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)
            updated = JobStore.get_by_id(job.id)

            assert result["error"] == "boom"
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "boom"

    def test_run_quality_checks_marks_failed_on_exception(self, app, owner_id, monkeypatch):
        with app.app_context():
            from transcria.config import get_config
            from transcria.jobs.store import JobStore
            from transcria.quality.quality_report import QualityReporter

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Quality Failure")
            runner = WorkflowRunner(JobStore, cfg)

            def raise_quality(*args, **kwargs):
                raise RuntimeError("quality boom")

            monkeypatch.setattr(QualityReporter, "run_all_checks", raise_quality)

            result = runner.run_quality_checks(job, cfg)
            updated = JobStore.get_by_id(job.id)

            assert result["error"] == "quality boom"
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "quality boom"

    def test_build_export_marks_failed_on_error_result(self, app, owner_id, monkeypatch):
        with app.app_context():
            from transcria.config import get_config
            from transcria.exports.package_builder import PackageBuilder
            from transcria.jobs.store import JobStore

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Export Failure")
            runner = WorkflowRunner(JobStore, cfg)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)
            monkeypatch.setattr(PackageBuilder, "build_package", lambda self, job: {"error": "zip boom"})

            result = runner.build_export(job, cfg)
            updated = JobStore.get_by_id(job.id)

            assert result["error"] == "zip boom"
            assert updated.state == JobState.FAILED.value
            assert updated.error_message == "zip boom"


class TestWorkflowSteps:
    def test_internal_steps_match_displayed_workflow(self):
        displayed_ids = [step["id"] for step in WorkflowState.get_steps()]
        helper_ids = []
        current = displayed_ids[0]
        while current is not None:
            helper_ids.append(current)
            current = WorkflowSteps.get_next_step_id(current)

        assert helper_ids == displayed_ids
        assert "speakers" not in helper_ids

    def test_participants_step_requires_upload_because_it_includes_speakers(self):
        assert WorkflowSteps.step_requires_upload("participants") is True

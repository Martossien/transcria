import pytest

from transcria.workflow.states import WorkflowState, StepStatus
from transcria.workflow.steps import WorkflowSteps
from transcria.jobs.models import JobState
from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.transitions import can_start_processing, next_preprocessing_state


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

    def test_compute_statuses_failed_uses_last_state(self):
        statuses = WorkflowState.compute_statuses("failed", "summary_running")
        assert statuses["file"] == StepStatus.DONE
        assert statuses["analyze"] == StepStatus.DONE
        assert statuses["summary"] == StepStatus.ERROR

    def test_compute_statuses_cancelled_uses_last_state(self):
        statuses = WorkflowState.compute_statuses("cancelled", "quality_checking")
        assert statuses["processing"] == StepStatus.DONE
        assert statuses["quality"] == StepStatus.SKIPPED

    def test_compute_statuses_failed_without_history_marks_first_step(self):
        statuses = WorkflowState.compute_statuses("failed")
        assert statuses["file"] == StepStatus.ERROR

    def test_compute_statuses_cancelled_without_history_marks_first_step(self):
        statuses = WorkflowState.compute_statuses("cancelled")
        assert statuses["file"] == StepStatus.SKIPPED

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


class TestWorkflowTransitions:
    def test_can_start_processing_accepts_retryable_states(self):
        assert can_start_processing(JobState.READY_TO_PROCESS.value) is True
        assert can_start_processing(JobState.TRANSCRIBING.value) is True
        assert can_start_processing(JobState.CANCELLED.value) is True

    def test_can_start_processing_rejects_preprocessing_states(self):
        assert can_start_processing(JobState.UPLOADED.value) is False
        assert can_start_processing(JobState.SUMMARY_DONE.value) is False

    def test_next_preprocessing_state_moves_to_ready(self):
        assert next_preprocessing_state(JobState.LEXICON_DONE.value) == JobState.READY_TO_PROCESS
        assert next_preprocessing_state(JobState.PARTICIPANTS_DONE.value) == JobState.READY_TO_PROCESS

    def test_next_preprocessing_state_none_for_irrelevant_state(self):
        assert next_preprocessing_state(JobState.UPLOADED.value) is None


class TestCanStartProfile:
    """Lancement profile-aware (le code reflété par l'E2E)."""

    def _p(self, pid):
        from transcria.workflow.profiles import get_profile
        return get_profile(pid)

    def test_srt_express_lancable_des_analyzed(self):
        from transcria.workflow.transitions import can_start_profile
        # Profil léger : aucune validation humaine requise → lançable juste après l'analyse.
        assert can_start_profile(JobState.ANALYZED.value, self._p("srt_express")) is True

    def test_dossier_qualite_refuse_des_analyzed(self):
        from transcria.workflow.transitions import can_start_profile
        # Profil complet : exige résumé/contexte/participants/lexique → refusé si trop tôt.
        assert can_start_profile(JobState.ANALYZED.value, self._p("dossier_qualite")) is False

    def test_word_rapide_lancable_des_context_done(self):
        from transcria.workflow.transitions import can_start_profile
        assert can_start_profile(JobState.CONTEXT_DONE.value, self._p("word_rapide")) is True
        # mais pas avant le contexte
        assert can_start_profile(JobState.SUMMARY_DONE.value, self._p("word_rapide")) is False

    def test_etats_de_retry_toujours_autorises(self):
        from transcria.workflow.transitions import can_start_profile
        # Rétro-compatibilité : un état de re-lancement reste autorisé pour tout profil.
        assert can_start_profile(JobState.LEXICON_DONE.value, self._p("dossier_qualite")) is True
        assert can_start_profile(JobState.FAILED.value, self._p("srt_express")) is True

    def test_avant_analyse_refuse(self):
        from transcria.workflow.transitions import can_start_profile
        assert can_start_profile(JobState.UPLOADED.value, self._p("srt_express")) is False


class TestWorkflowRunner:
    def test_job_store_persists_last_non_terminal_state_before_failure(self, app, owner_id):
        with app.app_context():
            from transcria.jobs.store import JobStore

            job = JobStore.create_job(owner_id, "Failure State Memory")
            JobStore.update_state(job.id, JobState.SUMMARY_RUNNING)
            JobStore.update_state(job.id, JobState.FAILED, "boom")

            updated = JobStore.get_by_id(job.id)
            assert updated.get_extra_data().get("last_non_terminal_state") == JobState.SUMMARY_RUNNING.value

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
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

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

    def test_job_context_builder_uses_summary_llm_when_available(self, app, owner_id):
        with app.app_context():
            from transcria.config import get_config
            from transcria.context.job_context_builder import JobContextBuilder
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore

            cfg = get_config()
            job = JobStore.create_job(owner_id, "Summary Context")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json(
                "context/meeting_context.json",
                {"title": "Titre", "summary_llm": "# Résumé\n\nContenu utile"},
            )

            context = JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])

            assert context["meeting"]["summary_control"] == "# Résumé\n\nContenu utile"

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

"""Tests des phases QUALITÉ et EXPORT (workflow/phases/quality.py, export.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
from transcria.workflow.runner import WorkflowRunner
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore


def _default_config(**overrides):
    cfg = {
        "storage": {"jobs_dir": "/tmp/test_transcria_jobs"},
        "workflow": {
            "enable_quick_summary": True,
            "enable_speaker_detection": True,
            "enable_quality_mode": True,
            "summary_llm": {"enabled": False},
            "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
        },
        "services": {
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "arbitrage_llm_port": 8080,
            "vllm_port": 8000,
        },
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


class TestWorkflowRunnerRunQualityChecks:
    def test_run_quality_checks_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Quality OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.quality.quality_report import QualityReporter

            fake_report = {"quality_score": 85, "total_checks": 5, "checks": []}
            monkeypatch.setattr(QualityReporter, "run_all_checks", lambda self, job: fake_report)

            result = runner.run_quality_checks(job, cfg)
            assert result["quality_score"] == 85

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.QUALITY_CHECKED.value


class TestWorkflowRunnerBuildExport:
    def test_build_export_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Export OK")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.exports.package_builder import PackageBuilder

            fake_result = {"zip_path": "/tmp/test.zip", "zip_name": "test.zip", "size_mb": 1.0}
            monkeypatch.setattr(PackageBuilder, "build_package", lambda self, job: fake_result)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            result = runner.build_export(job, cfg)
            assert "zip_path" in result

            updated = JobStore.get_by_id(job.id)
            assert updated.state == JobState.EXPORT_READY.value


class TestQualityPhaseBranches:
    def test_light_profile_uses_light_report(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Qualité légère")
            runner = WorkflowRunner(JobStore, cfg)

            from types import SimpleNamespace

            from transcria.quality import light_report
            from transcria.workflow import profiles

            monkeypatch.setattr(profiles, "profile_for_job",
                                lambda job: SimpleNamespace(run_quality="light"))
            monkeypatch.setattr(light_report, "run_light_quality",
                                lambda job, config: {"success": True, "mode": "light"})

            result = runner.run_quality_checks(job, cfg)

            assert result == {"success": True, "mode": "light"}
            assert JobStore.get_by_id(job.id).state == JobState.QUALITY_CHECKED.value


class TestEnrichSttCorpusQuality:
    def test_disabled_does_nothing(self, app, owner_id, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"stt_corpus": {"enabled": False}},
            )
            job = JobStore.create_job(owner_id, "Corpus off")
            # Aucun fichier requis : le drapeau coupe avant toute lecture.
            WorkflowRunner(JobStore, cfg)._enrich_stt_corpus_quality(job, cfg)

    def test_enriches_corpus_and_records_summary(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Corpus enrichi")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt import corpus as corpus_module

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_json("metadata/stt_corpus.json", [{"text": "bonjour"}])
            fs.save_json("metadata/transcription_segments.json", [{"text": "bonjour"}])
            fs.save_text("metadata/transcription_corrigee.srt",
                         "1\n00:00:00,000 --> 00:00:02,000\nBonjour\n")

            monkeypatch.setattr(corpus_module, "parse_srt_blocks", lambda text: [{"text": "Bonjour"}])
            monkeypatch.setattr(
                corpus_module, "enrich_corpus_with_quality",
                lambda corpus, raw, blocks: 1,
            )
            monkeypatch.setattr(
                corpus_module, "summarize_corpus",
                lambda corpus: {"quality_measure_mean": 0.12},
            )

            runner._enrich_stt_corpus_quality(job, cfg)

            extra = JobStore.get_by_id(job.id).get_extra_data() or {}
            assert extra["stt_corpus_summary"] == {"quality_measure_mean": 0.12}

    def test_missing_artifacts_are_ignored(self, app, owner_id, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Corpus incomplet")
            # Fichiers absents → sortie silencieuse, jamais d'exception (best-effort).
            WorkflowRunner(JobStore, cfg)._enrich_stt_corpus_quality(job, cfg)
            extra = JobStore.get_by_id(job.id).get_extra_data() or {}
            assert "stt_corpus_summary" not in extra

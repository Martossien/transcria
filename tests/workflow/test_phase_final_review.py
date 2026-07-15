"""Tests de la phase RELECTURE FINALE (workflow/phases/final_review.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
from transcria.workflow.runner import WorkflowRunner
from transcria.jobs.filesystem import JobFilesystem


class TestApplyFinalReviewStructuredDataNormalisation:
    """Le JSON relu par la relecture finale est normalisé en « listes de chaînes »
    (contrat du DOCX/UI) — stocké brut, un item dict faisait planter le rapport DOCX."""

    def test_items_dicts_normalises_en_chaines(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-fr-norm")
        fs.save_text("metadata/transcription_corrigee.srt", "1\n00:00:01,000 --> 00:00:02,000\nA: ok\n")
        fs.save_json("context/meeting_context.json", {"title": "T"})

        result = {
            "reviewed_structured_data": (
                '{"decisions": [{"objet": "budget", "resultat": "adopté"}, "Décision B"],'
                ' "votes": "Vote unique : adopté", "prochaine_date": "2026-07-01"}'
            ),
        }
        applied = WorkflowRunner._apply_final_review(fs, result)

        assert applied["structured_data_updated"] is True
        sd = (fs.load_json("context/meeting_context.json") or {}).get("structured_data") or {}
        assert all(isinstance(item, str) for item in sd.get("decisions", []))
        assert "Décision B" in sd["decisions"]
        assert sd["votes"] == ["Vote unique : adopté"]  # scalaire → liste de chaînes
        assert sd["prochaine_date"] == "2026-07-01"


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


class TestRunFinalReview:
    """Chemins de run_final_review — best-effort par contrat : toujours success=True."""

    _SRT = "1\n00:00:00,000 --> 00:00:05,000\nBonjour à tous\n"

    def _prepared(self, cfg, owner_id, monkeypatch, with_material=True):
        job = JobStore.create_job(owner_id, "Relecture finale")
        runner = WorkflowRunner(JobStore, cfg)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription_corrigee.srt", self._SRT)
        if with_material:
            fs.save_json("context/meeting_context.json", {"summary_llm": "# Synthèse\nBudget validé."})
        monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=300: True)
        monkeypatch.setattr(runner.allocator, "release_llm", lambda job_id: None)
        monkeypatch.setattr(runner.allocator, "release_phase", lambda job_id, phase: None)
        return job, runner, fs

    def test_skipped_when_arbitration_llm_disabled(self, app, owner_id, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={"arbitration_llm": {"enabled": False}},
            )
            job = JobStore.create_job(owner_id, "Relecture off")
            result = WorkflowRunner(JobStore, cfg).run_final_review(job, cfg)
            assert result == {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

    def test_skipped_without_corrected_srt(self, app, owner_id, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Relecture sans SRT")
            result = WorkflowRunner(JobStore, cfg).run_final_review(job, cfg)
            assert result["skipped"] is True and result["reason"] == "no_corrected_srt"

    def test_skipped_when_nothing_to_review(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch, with_material=False)
            result = runner.run_final_review(job, cfg)
            assert result["reason"] == "nothing_to_review"

    def test_skipped_retryable_when_llm_lock_busy(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner.allocator, "try_acquire_llm", lambda job_id, timeout_s=300: False)
            result = runner.run_final_review(job, cfg)
            assert result == {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

    def test_skipped_retryable_on_vram_shortage(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(runner.allocator, "try_reserve_llm", lambda job_id, mb, phase: False)
            result = runner.run_final_review(job, cfg)
            assert result["retryable"] is True and result["reason"] == "vram_insufficient"

    def test_skipped_retryable_when_llm_unavailable(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: False)
            result = runner.run_final_review(job, cfg)
            assert result["retryable"] is True and result["reason"] == "llm_unavailable"

    def test_success_applies_reviewed_outputs(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.gpu.opencode_runner import OpenCodeRunner

            reviewed = "1\n00:00:00,000 --> 00:00:05,000\nBonjour à toutes\n"

            def fake_review(self_runner, srt, summary, glossary, structured, **_kw):
                return {
                    "reviewed_srt": reviewed,
                    "harmonized_summary": "# Synthèse harmonisée",
                    "reviewed_structured_data": '{"decisions": ["valider le budget"]}',
                    "report": "# Rapport de relecture",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_final_review", fake_review)

            result = runner.run_final_review(job, cfg)

            assert result["success"] is True and result["review_applied"] is True
            assert result["srt_updated"] and result["summary_harmonized"] and result["structured_data_updated"]
            assert fs.load_text("metadata/transcription_corrigee.srt") == reviewed
            ctx = fs.load_json("context/meeting_context.json")
            assert ctx["summary_harmonized"] == "# Synthèse harmonisée"
            assert ctx["structured_data"]["decisions"] == ["valider le budget"]
            assert fs.load_text("metadata/final_review_report.md") == "# Rapport de relecture"

    def test_exception_is_best_effort_and_stops_llm_started_by_this_call(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job, runner, fs = self._prepared(cfg, owner_id, monkeypatch)
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            stopped = {"v": False}
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: stopped.update(v=True))

            from transcria.gpu.opencode_runner import OpenCodeRunner

            monkeypatch.setattr(
                OpenCodeRunner, "run_final_review",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("opencode HS")),
            )

            result = runner.run_final_review(job, cfg)

            assert result["success"] is True  # best-effort : le pipeline poursuit
            assert result["review_applied"] is False and "opencode HS" in result["error"]
            assert stopped["v"] is True  # LLM lancée par ce call → stoppée

"""Points d'accroche du magasin de fichiers PG (docs/STOCKAGE_PARTAGE_JOBS.md §5).

Vérifie que chaque tier pousse/tire au bon moment : upload et enfilage (frontale),
début/fin d'exécution et purge terminale (worker), checkpoint de phase (pipeline),
hooks web (pull paresseux / push après écriture), reconstruction du package, doctor.
"""
from __future__ import annotations

import pytest

from transcria.config.config_schema import validate_config
from transcria.diagnostics.doctor import FAIL, OK, WARN, check_shared_storage
from transcria.jobs import artifact_store
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.services.job_executor import JobExecutorService


@pytest.fixture
def pg_cfg(app, tmp_path):
    """Config backend `pg` sur un jobs_dir isolé (la base de test est déjà PostgreSQL)."""
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs"), "shared_backend": "pg"},
        "workflow": {"queue": {"enabled": False}},
    }


@pytest.fixture
def recorder(monkeypatch):
    """Trace les appels artifact_store faits par les hooks (sans toucher la base)."""
    calls: list[tuple] = []

    def rec(name):
        def _f(cfg, job_id, **kw):
            calls.append((name, job_id, tuple(kw.get("prefixes") or ())))
            return {"backend": "pg", "pushed": 0, "pulled": 0, "skipped": 0, "bytes": 0}
        return _f

    for mod in ("transcria.services.job_executor", "transcria.services.pipeline_service"):
        monkeypatch.setattr(f"{mod}.artifact_store.push_job_files", rec("push"), raising=True)
        monkeypatch.setattr(f"{mod}.artifact_store.pull_job_files", rec("pull"), raising=True)
    monkeypatch.setattr(
        "transcria.services.job_executor.artifact_store.purge_input_files",
        lambda cfg, job_id: calls.append(("purge_input", job_id, ())) or 0,
    )
    return calls


def _make_job(app, owner_id, state=JobState.READY_TO_PROCESS):
    with app.app_context():
        job = JobStore.create_job(owner_id, "Job split")
        JobStore.update_state(job.id, state)
        return job.id


class TestExecutorHooks:
    def test_run_process_pulls_then_pushes_and_purges_on_completed(
        self, app, owner_id, pg_cfg, recorder, monkeypatch
    ):
        monkeypatch.setattr(
            "transcria.services.pipeline_service.PipelineService.run_process",
            lambda self, job, audio_path, mode, finalize_job_state=False: {"status": "completed"},
        )
        monkeypatch.setattr("transcria.services.job_executor._notify", lambda *a, **k: None)
        svc = JobExecutorService(app, pg_cfg)
        job_id = _make_job(app, owner_id)
        try:
            svc._run_process(job_id, "/tmp/a.wav", "fast")
        finally:
            svc._executor.shutdown(wait=False)

        names = [c[0] for c in recorder]
        assert names == ["pull", "push", "purge_input"]
        assert all(c[1] == job_id for c in recorder)

    def test_step_mode_does_not_purge_input(self, app, owner_id, pg_cfg, recorder, monkeypatch):
        """Après une étape `summary`, l'audio resservira au worker : pas de purge."""
        monkeypatch.setattr(
            "transcria.workflow.runner.WorkflowRunner.run_summary",
            lambda self, job, audio_path, cfg: {"status": "ok"},
        )
        svc = JobExecutorService(app, pg_cfg)
        job_id = _make_job(app, owner_id, state=JobState.UPLOADED)
        try:
            svc._run_process(job_id, "/tmp/a.wav", "summary")
        finally:
            svc._executor.shutdown(wait=False)

        names = [c[0] for c in recorder]
        assert names == ["pull", "push"]  # pas de purge_input

    def test_vram_wait_does_not_purge_input(self, app, owner_id, pg_cfg, recorder, monkeypatch):
        """Re-queue transitoire (vram_wait) : surtout ne pas purger les entrées."""
        monkeypatch.setattr(
            "transcria.services.pipeline_service.PipelineService.run_process",
            lambda self, job, audio_path, mode, finalize_job_state=False: {
                "vram_wait": True, "required_mb": 6000, "phase": "stt", "retry_after_s": 30,
            },
        )
        monkeypatch.setattr("transcria.services.job_executor.alert_admin_vram_wait", lambda *a, **k: None)
        svc = JobExecutorService(app, pg_cfg)
        job_id = _make_job(app, owner_id)
        try:
            with app.app_context():
                from transcria.queue.store import QueueStore
                QueueStore.enqueue(job_id, mode="fast")
            svc._run_process(job_id, "/tmp/a.wav", "fast")
        finally:
            svc._executor.shutdown(wait=False)

        assert "purge_input" not in [c[0] for c in recorder]

    def test_submit_process_pushes_inputs_before_enqueue(self, app, owner_id, tmp_path, monkeypatch):
        pushes: list[tuple] = []
        monkeypatch.setattr(
            "transcria.services.job_executor.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: pushes.append((job_id, tuple(kw.get("prefixes") or ()))),
        )
        cfg = {
            "storage": {"jobs_dir": str(tmp_path), "shared_backend": "pg"},
            "workflow": {"queue": {"enabled": True, "poll_interval_s": 300}},
        }
        svc = JobExecutorService(app, cfg, run_scheduler=False)
        job_id = _make_job(app, owner_id)
        try:
            with app.app_context():
                result = svc.submit_process(job_id, "/tmp/a.wav", "fast")
            assert result["accepted"] is True
            assert pushes == [(job_id, artifact_store.INPUT_PREFIXES)]
        finally:
            with app.app_context():
                from transcria.queue.store import QueueStore
                QueueStore.dequeue(job_id, status="cancelled")
            svc._executor.shutdown(wait=False)


class _SL:
    """Logger structuré factice (même stub que test_pipeline_resume.py)."""

    def set_context(self, **k):
        pass

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _stub_preprocess(svc, monkeypatch, on_transform=None):
    """Neutralise les transforms audio (pas de GPU, pas d'IO réelle).

    NB : on appelle `_run_pipeline_steps` directement (comme test_pipeline_resume.py)
    pour ne PAS passer par le `finally` de run_process qui arrête la VRAIE LLM
    d'arbitrage (interdit en test)."""
    def _preflight(*a, **k):
        if on_transform:
            on_transform()
        return {}

    monkeypatch.setattr(svc, "_run_audio_preflight", _preflight)
    monkeypatch.setattr(svc, "_run_audio_scene_analysis", lambda *a, **k: {})
    monkeypatch.setattr(svc, "_refresh_audio_quality_with_scene", lambda *a, **k: None)
    for m in ("_run_source_separation", "_run_audio_scene_filter",
              "_run_audio_denoise", "_run_audio_normalization"):
        monkeypatch.setattr(svc, m, lambda job, audio, *a, **k: audio)
    monkeypatch.setattr(svc, "_define_pipeline_steps", lambda job, audio_path, mode: [])
    monkeypatch.setattr(svc.runner, "run_transcription", lambda job, audio_path, cfg: {"segments": []})


class TestPipelineCheckpoint:
    def test_push_happens_before_phase_marker(self, app, owner_id, pg_cfg, monkeypatch):
        """Sémantique du checkpoint : artefacts durables en base AVANT le marqueur."""
        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow import resume

        events: list[str] = []
        monkeypatch.setattr(
            "transcria.services.pipeline_service.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: events.append("push"),
        )
        orig_mark = resume.mark_phase_done
        monkeypatch.setattr(
            "transcria.workflow.resume.mark_phase_done",
            lambda store, job_id, phase, fingerprints=None: (
                events.append(f"mark:{phase}"),
                orig_mark(store, job_id, phase, fingerprints),
            ),
        )

        job_id = _make_job(app, owner_id)
        with app.app_context():
            job = JobStore.get_by_id(job_id)
            svc = PipelineService(pg_cfg)
            _stub_preprocess(svc, monkeypatch)
            result = svc._run_pipeline_steps(job, "/tmp/a.wav", "fast", _SL(), finalize_job_state=False)

        assert result.get("status") == "completed"
        assert events == ["push", "mark:preprocess", "push", "mark:transcription"]

    def test_push_failure_leaves_phase_unmarked(self, app, owner_id, pg_cfg, monkeypatch):
        """Push impossible → la phase n'est PAS marquée faite (sera rejouée)."""
        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow import resume

        def boom(cfg, job_id, **kw):
            raise RuntimeError("base injoignable")
        monkeypatch.setattr("transcria.services.pipeline_service.artifact_store.push_job_files", boom)

        job_id = _make_job(app, owner_id)
        with app.app_context():
            job = JobStore.get_by_id(job_id)
            svc = PipelineService(pg_cfg)
            _stub_preprocess(svc, monkeypatch)
            with pytest.raises(RuntimeError):
                svc._run_pipeline_steps(job, "/tmp/a.wav", "fast", _SL(), finalize_job_state=False)
            assert resume.get_completed_phases(JobStore.get_by_id(job_id)) == []

    def test_preprocess_replayed_when_resumed_audio_missing(self, app, owner_id, pg_cfg, monkeypatch):
        """Reprise sur un autre worker : chemin audio mémorisé absent → transforms rejoués."""
        from transcria.services.pipeline_service import PipelineService
        from transcria.workflow import resume

        replayed: list[str] = []
        job_id = _make_job(app, owner_id)
        with app.app_context():
            resume.mark_phase_done(JobStore, job_id, "preprocess")
            resume.set_processed_audio_path(JobStore, job_id, "/disque/autre-worker/vocals.wav")
            job = JobStore.get_by_id(job_id)
            svc = PipelineService(pg_cfg)
            _stub_preprocess(svc, monkeypatch, on_transform=lambda: replayed.append("transform"))
            result = svc._run_pipeline_steps(job, "/tmp/a.wav", "fast", _SL(), finalize_job_state=False)

        assert result.get("status") == "completed"
        assert replayed  # les transforms ont été rejoués au lieu d'utiliser un chemin mort


class TestWebHooks:
    def test_lazy_pull_on_job_request(self, app, admin_client, owner_id, monkeypatch):
        pulls: list[str] = []
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.is_pg_backend", lambda cfg: True
        )
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.pull_job_files_throttled",
            lambda cfg, job_id, **kw: pulls.append(job_id),
        )
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: None,
        )
        job_id = _make_job(app, owner_id, state=JobState.UPLOADED)
        resp = admin_client.get(f"/api/jobs/{job_id}/status")
        assert resp.status_code == 200
        assert pulls == [job_id]

    def test_push_after_successful_write(self, app, admin_client, owner_id, monkeypatch):
        pushes: list[tuple] = []
        monkeypatch.setattr("transcria.web.routes.artifact_store.is_pg_backend", lambda cfg: True)
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.pull_job_files_throttled",
            lambda cfg, job_id, **kw: None,
        )
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: pushes.append((job_id, tuple(kw.get("prefixes") or ()))),
        )
        job_id = _make_job(app, owner_id, state=JobState.SUMMARY_DONE)
        resp = admin_client.post(f"/api/jobs/{job_id}/context", json={"brief": "invitation"})
        assert resp.status_code == 200
        # WEB_WRITE_PREFIXES (jamais input/) : ne pas annuler la purge terminale.
        assert pushes == [(job_id, artifact_store.WEB_WRITE_PREFIXES)]
        assert "input/" not in artifact_store.WEB_WRITE_PREFIXES

    def test_no_pull_for_unauthenticated_request(self, app, client, owner_id, monkeypatch):
        """Pas de travail (SELECT par job_id arbitraire) pour un anonyme."""
        pulls: list[str] = []
        monkeypatch.setattr("transcria.web.routes.artifact_store.is_pg_backend", lambda cfg: True)
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.pull_job_files_throttled",
            lambda cfg, job_id, **kw: pulls.append(job_id),
        )
        job_id = _make_job(app, owner_id, state=JobState.UPLOADED)
        client.get(f"/api/jobs/{job_id}/status")  # non connecté → 401/redirect
        assert pulls == []

    def test_no_push_after_read_or_failed_write(self, app, admin_client, owner_id, monkeypatch):
        pushes: list[str] = []
        monkeypatch.setattr("transcria.web.routes.artifact_store.is_pg_backend", lambda cfg: True)
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.pull_job_files_throttled",
            lambda cfg, job_id, **kw: None,
        )
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: pushes.append(job_id),
        )
        job_id = _make_job(app, owner_id, state=JobState.UPLOADED)
        assert admin_client.get(f"/api/jobs/{job_id}/status").status_code == 200
        assert admin_client.get("/api/jobs/introuvable/status").status_code == 404
        assert pushes == []


class TestJobServiceHooks:
    def test_upload_pushes_input_blobs(self, app, owner_id, tmp_path, monkeypatch):
        pushes: list[tuple] = []
        monkeypatch.setattr(
            "transcria.jobs.artifact_store.push_job_files",
            lambda cfg, job_id, **kw: pushes.append((job_id, tuple(kw.get("prefixes") or ()))),
        )
        from transcria.services.job_service import JobService
        with app.app_context():
            job = JobStore.create_job(owner_id, "Upload split")
            JobService.upload(job.id, b"contenu audio", "reunion.mp3", str(tmp_path))
            assert pushes == [(job.id, ("input/",))]

    def test_delete_purges_blobs(self, app, owner_id, tmp_path, monkeypatch):
        deleted: list[str] = []
        monkeypatch.setattr(
            "transcria.jobs.artifact_store.delete_job_files",
            lambda job_id: deleted.append(job_id) or 0,
        )
        from transcria.services.job_service import JobService
        with app.app_context():
            job = JobStore.create_job(owner_id, "Delete split")
            assert JobService.delete(job.id, str(tmp_path)) is True
            assert deleted == [job.id]


class TestPackageRebuild:
    def test_download_package_rebuilds_locally_in_pg_mode(self, app, admin_client, owner_id, monkeypatch):
        """Backend pg : le zip (exclu de la synchro) est reconstruit localement à la demande."""
        monkeypatch.setattr("transcria.web.routes.artifact_store.is_pg_backend", lambda cfg: True)
        monkeypatch.setattr(
            "transcria.web.routes.artifact_store.pull_job_files_throttled",
            lambda cfg, job_id, **kw: None,
        )
        job_id = _make_job(app, owner_id, state=JobState.COMPLETED)
        with app.app_context():
            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:01,000\nBonjour\n")

        resp = admin_client.get(f"/api/jobs/{job_id}/download/package")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        assert len(resp.data) > 0


class TestSchedulerMaterialization:
    """Worker neuf / cache vidé : le dispatch matérialise input/ avant de conclure
    « audio introuvable » (sinon le job passerait failed sans même tenter le pull)."""

    def _scheduler(self, app, tmp_path, backend):
        from transcria.queue.scheduler import QueueScheduler
        cfg = {
            "storage": {"jobs_dir": str(tmp_path), "shared_backend": backend},
            "workflow": {"queue": {"enabled": True, "poll_interval_s": 300}},
        }
        return QueueScheduler(app, cfg, lambda *a: None)  # jamais démarré (pas de thread)

    def test_materializes_audio_from_store(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path, "pg")

        def fake_pull(cfg, job_id, prefixes=None, **kw):
            assert tuple(prefixes) == ("input/",)
            dest = tmp_path / job_id / "input"
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "original.mp3").write_bytes(b"audio")

        monkeypatch.setattr("transcria.jobs.artifact_store.pull_job_files", fake_pull)
        path = sched._materialize_job_inputs("job-neuf")
        assert path is not None and path.name == "original.mp3"

    def test_noop_in_fs_backend(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path, "fs")
        monkeypatch.setattr(
            "transcria.jobs.artifact_store.pull_job_files",
            lambda *a, **k: pytest.fail("pull ne doit pas être appelé en backend fs"),
        )
        assert sched._materialize_job_inputs("job-x") is None

    def test_pull_error_returns_none(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path, "pg")

        def boom(*a, **k):
            raise RuntimeError("base indisponible")
        monkeypatch.setattr("transcria.jobs.artifact_store.pull_job_files", boom)
        assert sched._materialize_job_inputs("job-x") is None

    def test_blob_absent_returns_none(self, app, tmp_path, monkeypatch):
        sched = self._scheduler(app, tmp_path, "pg")
        monkeypatch.setattr("transcria.jobs.artifact_store.pull_job_files", lambda *a, **k: None)
        assert sched._materialize_job_inputs("job-x") is None


class TestStartupGuard:
    def test_pg_backend_on_sqlite_refuses_to_start(self):
        cfg = {"storage": {"shared_backend": "pg"}}
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            artifact_store.assert_runtime_compatible(cfg, "sqlite")

    def test_pg_backend_on_postgresql_ok(self):
        artifact_store.assert_runtime_compatible({"storage": {"shared_backend": "pg"}}, "postgresql")

    def test_fs_backend_any_dialect_ok(self):
        artifact_store.assert_runtime_compatible({"storage": {"shared_backend": "fs"}}, "sqlite")
        artifact_store.assert_runtime_compatible({}, "sqlite")


class TestDoctorAndSchema:
    def test_doctor_warns_on_split_role_with_fs(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
        cfg = {"runtime": {"role": "web"}, "storage": {"shared_backend": "fs"}}
        assert check_shared_storage(cfg).status == WARN

    def test_doctor_ok_all_in_one_fs(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
        cfg = {"runtime": {"role": "all"}, "storage": {"shared_backend": "fs"}}
        assert check_shared_storage(cfg).status == OK

    def test_doctor_ok_pg_backend(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
        monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
        cfg = {
            "runtime": {"role": "scheduler"},
            "storage": {"shared_backend": "pg", "database_url": "postgresql+psycopg://u@h/db"},
        }
        assert check_shared_storage(cfg, table_exists=lambda uri: True).status == OK

    def test_doctor_fails_pg_backend_when_tables_missing(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
        cfg = {"storage": {"shared_backend": "pg", "database_url": "postgresql+psycopg://u@h/db"}}
        result = check_shared_storage(cfg, table_exists=lambda uri: False)
        assert result.status == FAIL
        assert "job_files" in result.detail

    def test_doctor_fails_pg_backend_when_db_unreachable(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
        cfg = {"storage": {"shared_backend": "pg", "database_url": "postgresql+psycopg://u@h/db"}}

        def boom(uri):
            raise ConnectionError("refusée")
        assert check_shared_storage(cfg, table_exists=boom).status == FAIL

    def test_doctor_fails_pg_backend_without_postgres(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
        cfg = {"storage": {"shared_backend": "pg", "database_url": "sqlite:///x.db"}}
        assert check_shared_storage(cfg).status == FAIL

    def test_schema_rejects_invalid_backend(self):
        result = validate_config({"storage": {"jobs_dir": "./jobs", "database_url": "sqlite:///x.db",
                                                   "shared_backend": "nfs"}})
        assert any("shared_backend" in e for e in result.errors)

    def test_schema_rejects_pg_backend_on_sqlite(self):
        result = validate_config({"storage": {"jobs_dir": "./jobs", "database_url": "sqlite:///x.db",
                                                   "shared_backend": "pg"}})
        assert any("PostgreSQL" in e for e in result.errors)

"""Edge case and error handling tests for Web API."""
import io
import json
import copy
import uuid


class TestAuthEdgeCases:
    def test_login_empty_username(self, client):
        r = client.post("/login", data={"username": "", "password": "test"})
        assert r.status_code == 401

    def test_login_empty_password(self, client):
        r = client.post("/login", data={"username": "admin", "password": ""})
        assert r.status_code == 401

    def test_login_nonexistent_user(self, client):
        r = client.post("/login", data={"username": "nobody_xyz_123", "password": "x"})
        assert r.status_code == 401

    def test_login_deactivated_user(self, app, client):
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.auth.models import Role
            import uuid
            uname = f"deact_{uuid.uuid4().hex[:6]}"
            u = UserStore.create_user(username=uname, password="pw", role=Role.OPERATOR)
            UserStore.deactivate_user(u.id)
        r = client.post("/login", data={"username": uname, "password": "pw"})
        assert r.status_code == 401

    def test_protected_page_without_login(self, client):
        for path in ["/", "/admin/users", "/system", "/jobs/nonexistent"]:
            r = client.get(path)
            assert r.status_code in (302, 401, 404)

    def test_login_then_logout_then_protected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)
        r = client.get("/")
        assert r.status_code == 200
        client.get("/logout")
        r = client.get("/")
        assert r.status_code == 302


class TestUserManagementEdgeCases:
    def test_create_duplicate_user(self, admin_client, app):
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.auth.models import Role
            existing = UserStore.get_by_username("duptest")
            if not existing:
                UserStore.create_user(username="duptest", password="pw", role=Role.OPERATOR)
        r = admin_client.post(
            "/admin/users/new",
            data={"username": "duptest", "password": "pw2", "role": "operator"},
            follow_redirects=True,
        )
        assert r.status_code == 200

    def test_create_user_invalid_role(self, admin_client):
        r = admin_client.post(
            "/admin/users/new",
            data={"username": "badrole", "password": "pw", "role": "superadmin"},
            follow_redirects=True,
        )
        assert r.status_code == 200

    def test_edit_nonexistent_user(self, admin_client):
        r = admin_client.get("/admin/users/fake-uuid-12345/edit")
        assert r.status_code == 302

    def test_operator_cannot_manage_users(self, operator_client):
        r = operator_client.get("/admin/users")
        assert r.status_code == 403
        r = operator_client.get("/admin/users/new")
        assert r.status_code == 403


class TestJobEdgeCases:
    def test_get_nonexistent_job(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "T1"}, follow_redirects=True)
        assert r.status_code == 200

    def test_upload_no_file(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "U1"}, follow_redirects=True)
        path = r.request.path
        jid = path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/upload", data={})
        assert r.status_code in (400, 404)
        data = json.loads(r.data)
        assert "error" in data

    def test_upload_invalid_extension(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Bad"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        data = io.BytesIO(b"not audio")
        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (data, "file.exe")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400

    def test_upload_preserves_custom_job_title(self, admin_client, app):
        r = admin_client.post("/jobs/new", data={"title": "Comité direction Q1"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (io.BytesIO(b"fake audio"), "enregistrement_20260505.mp3")},
            content_type="multipart/form-data",
        )

        assert r.status_code == 200
        with app.app_context():
            from transcria.jobs.store import JobStore

            assert JobStore.get_by_id(jid).title == "Comité direction Q1"

    def test_upload_uses_filename_stem_for_default_title(self, admin_client, app):
        r = admin_client.post("/jobs/new", data={"title": ""}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (io.BytesIO(b"fake audio"), "reunion_audio.mp3")},
            content_type="multipart/form-data",
        )

        assert r.status_code == 200
        with app.app_context():
            from transcria.jobs.store import JobStore

            assert JobStore.get_by_id(jid).title == "reunion_audio"

    def test_create_job_sanitizes_title(self, admin_client, app):
        r = admin_client.post("/jobs/new", data={"title": "<script>Budget\x00Q1</script>"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        assert r.status_code == 200
        with app.app_context():
            from transcria.jobs.store import JobStore

            assert JobStore.get_by_id(jid).title == "scriptBudgetQ1/script"

    def test_upload_rejected_after_job_has_file(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Already uploaded"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (io.BytesIO(b"first"), "first.mp3")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200

        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (io.BytesIO(b"second"), "second.mp3")},
            content_type="multipart/form-data",
        )

        assert r.status_code == 400
        assert "déjà" in json.loads(r.data)["error"]

    def test_analyze_without_audio(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "NoAudio"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/analyze")
        assert r.status_code == 400

    def test_summary_without_audio(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "NoAudio2"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/summary")
        assert r.status_code == 400

    def test_context_invalid_json(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Ctx"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/context", data="not json", content_type="application/json")
        assert r.status_code in (200, 400)


class TestJobAccessControl:
    def _create_operator_with_job(self, app, title_prefix="Access"):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            from transcria.jobs.store import JobStore

            suffix = uuid.uuid4().hex[:8]
            username = f"user_{suffix}"
            user = UserStore.create_user(username=username, password="pw", role=Role.OPERATOR)
            job = JobStore.create_job(user.id, f"{title_prefix} {suffix}")
            return username, "pw", user.id, job.id, job.title

    def _login(self, app, username, password):
        client = app.test_client()
        client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
        return client

    def test_operator_index_shows_only_own_jobs(self, app):
        owner_name, owner_pw, _, _, own_title = self._create_operator_with_job(app, "Own")
        _, _, _, _, other_title = self._create_operator_with_job(app, "Foreign")

        client = self._login(app, owner_name, owner_pw)
        r = client.get("/")

        assert r.status_code == 200
        assert own_title.encode() in r.data
        assert other_title.encode() not in r.data

    def test_admin_index_shows_all_jobs(self, app, admin_client):
        _, _, _, _, title_a = self._create_operator_with_job(app, "AdminVisibleA")
        _, _, _, _, title_b = self._create_operator_with_job(app, "AdminVisibleB")

        r = admin_client.get("/")

        assert r.status_code == 200
        assert title_a.encode() in r.data
        assert title_b.encode() in r.data

    def test_operator_cannot_open_foreign_job_page(self, app):
        owner_name, owner_pw, _, _, _ = self._create_operator_with_job(app, "Owner")
        _, _, _, foreign_job_id, _ = self._create_operator_with_job(app, "Foreign")

        client = self._login(app, owner_name, owner_pw)
        r = client.get(f"/jobs/{foreign_job_id}")

        assert r.status_code == 403

    def test_operator_cannot_call_foreign_job_apis(self, app):
        owner_name, owner_pw, _, _, _ = self._create_operator_with_job(app, "Owner")
        _, _, _, foreign_job_id, _ = self._create_operator_with_job(app, "Foreign")
        client = self._login(app, owner_name, owner_pw)

        endpoints = [
            ("post", f"/api/jobs/{foreign_job_id}/analyze", {}),
            ("post", f"/api/jobs/{foreign_job_id}/summary", {}),
            ("post", f"/api/jobs/{foreign_job_id}/context", {"json": {"title": "hack"}}),
            ("post", f"/api/jobs/{foreign_job_id}/participants", {"json": [{"name": "hack"}]}),
            ("post", f"/api/jobs/{foreign_job_id}/lexicon", {"json": [{"term": "hack"}]}),
            ("post", f"/api/jobs/{foreign_job_id}/speakers/detect", {}),
            ("post", f"/api/jobs/{foreign_job_id}/speakers/map", {"json": {"SPEAKER_00": "hack"}}),
            ("post", f"/api/jobs/{foreign_job_id}/process", {"json": {"mode": "fast"}}),
            ("post", f"/api/jobs/{foreign_job_id}/quality", {}),
            ("post", f"/api/jobs/{foreign_job_id}/export", {}),
            ("post", f"/api/jobs/{foreign_job_id}/push-to-editor", {}),
            ("get", f"/api/jobs/{foreign_job_id}/speakers/clips", {}),
        ]

        for method, path, kwargs in endpoints:
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 403, path
            assert json.loads(response.data)["error"] == "Accès interdit"

    def test_admin_can_delete_foreign_job(self, app, admin_client):
        _, _, _, job_id, _ = self._create_operator_with_job(app, "DeleteByAdmin")

        r = admin_client.post(f"/jobs/{job_id}/delete", follow_redirects=True)

        assert r.status_code == 200
        with app.app_context():
            from transcria.jobs.store import JobStore

            assert JobStore.get_by_id(job_id) is None

    def test_delete_job_respects_config_flag(self, app, admin_client):
        from transcria.config import get_config, set_config

        original_cfg = copy.deepcopy(get_config())
        cfg = copy.deepcopy(original_cfg)
        cfg.setdefault("security", {})["allow_job_delete"] = False
        set_config(cfg)
        try:
            _, _, _, job_id, _ = self._create_operator_with_job(app, "DeleteDisabled")
            r = admin_client.post(f"/jobs/{job_id}/delete", follow_redirects=True)

            assert r.status_code == 403
            with app.app_context():
                from transcria.jobs.store import JobStore

                assert JobStore.get_by_id(job_id) is not None
        finally:
            set_config(original_cfg)


class TestPipelineErrors:
    def _create_uploaded_job(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Pipeline"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(
            f"/api/jobs/{jid}/upload",
            data={"file": (io.BytesIO(b"fake audio"), "audio.mp3")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        return jid

    def test_process_stops_after_transcription_error(self, admin_client, monkeypatch):
        from transcria.workflow.runner import WorkflowRunner

        jid = self._create_uploaded_job(admin_client)
        called = {"correction": False}

        monkeypatch.setattr(WorkflowRunner, "run_transcription", lambda self, job, audio_path, cfg: {"error": "stt down"})

        def fail_if_called(self, job, cfg):
            called["correction"] = True
            return {"success": True}

        monkeypatch.setattr(WorkflowRunner, "run_correction", fail_if_called)

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 500
        assert json.loads(r.data)["step"] == "transcription"
        assert called["correction"] is False

    def test_process_stops_after_correction_error(self, admin_client, monkeypatch):
        from transcria.workflow.runner import WorkflowRunner

        jid = self._create_uploaded_job(admin_client)
        called = {"quality": False}

        monkeypatch.setattr(WorkflowRunner, "run_transcription", lambda self, job, audio_path, cfg: {"text": "ok"})
        monkeypatch.setattr(WorkflowRunner, "run_correction", lambda self, job, cfg: {"success": False, "error": "qwen down"})

        def fail_if_called(self, job, cfg):
            called["quality"] = True
            return {}

        monkeypatch.setattr(WorkflowRunner, "run_quality_checks", fail_if_called)

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 500
        assert json.loads(r.data)["step"] == "correction"
        assert called["quality"] is False


class TestApiErrorResponses:
    def test_nonexistent_job_upload(self, admin_client):
        r = admin_client.post("/api/jobs/fake-id-123/upload", data={})
        assert r.status_code == 404

    def test_nonexistent_job_analyze(self, admin_client):
        r = admin_client.post("/api/jobs/fake-id-123/analyze")
        assert r.status_code == 404

    def test_nonexistent_job_context(self, admin_client):
        r = admin_client.post("/api/jobs/fake-id-123/context", json={})
        assert r.status_code == 404

    def test_nonexistent_job_download(self, admin_client):
        r = admin_client.get("/api/jobs/fake-id-123/download/srt")
        assert r.status_code == 404
        r = admin_client.get("/api/jobs/fake-id-123/download/package")
        assert r.status_code == 404

    def test_viewer_cannot_create_job(self, viewer_client):
        r = viewer_client.post("/jobs/new", data={"title": "Nope"})
        assert r.status_code == 403

    def test_viewer_cannot_access_system(self, viewer_client):
        r = viewer_client.get("/system")
        assert r.status_code == 403


class TestSpeakerMappingEdgeCases:
    def test_map_speakers_empty(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Spk"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/speakers/map", json={})
        assert r.status_code == 200

    def test_map_speakers_nonexistent_job(self, admin_client):
        r = admin_client.post("/api/jobs/fake-job-speakers/map", json={"SPEAKER_00": "test"})
        assert r.status_code == 404


class TestLexiconEdgeCases:
    def test_lexicon_csv_import(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "LexCSV"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(
            f"/api/jobs/{jid}/lexicon",
            data="TERM1,technique,critique\nTERM2,projet,importante",
            content_type="text/plain",
        )
        assert r.status_code == 200

    def test_lexicon_skip_empty(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "LexSkip"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]
        r = admin_client.post(f"/api/jobs/{jid}/lexicon", json=[])
        assert r.status_code == 200

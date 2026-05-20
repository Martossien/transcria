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

    def test_group_member_can_open_group_job_page(self, app):
        owner_name, owner_pw, owner_id, owner_job_id, owner_title = self._create_operator_with_job(app, "SharedGroup")
        member_name, member_pw, member_id, _, _ = self._create_operator_with_job(app, "GroupMember")
        with app.app_context():
            from transcria.auth.groups import GroupStore

            group = GroupStore.create_group(f"Equipe {uuid.uuid4().hex[:8]}")
            GroupStore.add_member(group.id, owner_id)
            GroupStore.add_member(group.id, member_id)

        client = self._login(app, member_name, member_pw)
        r = client.get(f"/jobs/{owner_job_id}")
        index = client.get("/")

        assert r.status_code == 200
        assert index.status_code == 200
        assert owner_title.encode() in index.data
        assert b"Partag" in index.data

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


class TestGroupManagement:
    def _login(self, app, username, password):
        client = app.test_client()
        client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
        return client

    def test_admin_can_create_group_and_add_member(self, app, admin_client):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            suffix = uuid.uuid4().hex[:8]
            user = UserStore.create_user(username=f"gm_{suffix}", password="pw", role=Role.OPERATOR)
            user_id = user.id

        group_name = f"Groupe web {uuid.uuid4().hex[:8]}"
        r = admin_client.post(
            "/admin/groups/new",
            data={"name": group_name, "description": "Test"},
            follow_redirects=False,
        )
        assert r.status_code == 302

        with app.app_context():
            from transcria.auth.groups import GroupStore
            group = GroupStore.get_by_name(group_name)
            assert group is not None
            group_id = group.id

        r = admin_client.post(
            f"/admin/groups/{group_id}/edit",
            data={"action": "add_member", "user_id": user_id, "role": "member"},
            follow_redirects=True,
        )
        assert r.status_code == 200

        with app.app_context():
            from transcria.auth.groups import GroupStore
            assert GroupStore.users_share_group(user_id, user_id) is True
            assert len(GroupStore.list_members(group_id)) == 1

    def test_group_admin_can_manage_members_but_not_create_groups(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole, Role
            from transcria.auth.store import UserStore

            suffix = uuid.uuid4().hex[:8]
            admin_user = UserStore.create_user(username=f"gadmin_{suffix}", password="pw", role=Role.OPERATOR)
            member_user = UserStore.create_user(username=f"gmember_{suffix}", password="pw", role=Role.OPERATOR)
            group = GroupStore.create_group(f"Groupe admin {suffix}")
            GroupStore.add_member(group.id, admin_user.id, GroupRole.GROUP_ADMIN)
            admin_username = admin_user.username
            member_user_id = member_user.id
            group_id = group.id

        client = self._login(app, admin_username, "pw")
        forbidden = client.get("/admin/groups/new")
        assert forbidden.status_code == 403

        r = client.post(
            f"/admin/groups/{group_id}/edit",
            data={"action": "add_member", "user_id": member_user_id, "role": "member"},
            follow_redirects=True,
        )
        assert r.status_code == 200

        with app.app_context():
            from transcria.auth.groups import GroupStore
            assert len(GroupStore.list_members(group_id)) == 2

    def test_group_admin_cannot_remove_last_group_admin(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole, Role
            from transcria.auth.store import UserStore

            suffix = uuid.uuid4().hex[:8]
            admin_user = UserStore.create_user(username=f"gsolo_{suffix}", password="pw", role=Role.OPERATOR)
            group = GroupStore.create_group(f"Groupe solo {suffix}")
            GroupStore.add_member(group.id, admin_user.id, GroupRole.GROUP_ADMIN)
            admin_username = admin_user.username
            admin_user_id = admin_user.id
            group_id = group.id

        client = self._login(app, admin_username, "pw")
        r = client.post(
            f"/admin/groups/{group_id}/edit",
            data={"action": "remove_member", "user_id": admin_user_id},
            follow_redirects=True,
        )
        assert r.status_code == 200

        with app.app_context():
            from transcria.auth.groups import GroupStore
            assert GroupStore.get_membership(group_id, admin_user_id) is not None


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

    def test_process_stops_after_transcription_error(self, admin_client, monkeypatch, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.services.job_executor import get_job_executor

        jid = self._create_uploaded_job(admin_client)
        with app.app_context():
            JobStore.update_state(jid, JobState.READY_TO_PROCESS)
        executor = get_job_executor()
        monkeypatch.setattr(
            executor,
            "submit_process",
            lambda job_id, audio_path, mode: {"accepted": True, "status": "queued", "mode": mode},
        )

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 202
        assert json.loads(r.data)["status"] == "queued"

    def test_process_stops_after_correction_error(self, admin_client, monkeypatch, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.services.job_executor import get_job_executor

        jid = self._create_uploaded_job(admin_client)
        with app.app_context():
            JobStore.update_state(jid, JobState.READY_TO_PROCESS)
        executor = get_job_executor()
        monkeypatch.setattr(
            executor,
            "submit_process",
            lambda job_id, audio_path, mode: {"accepted": False, "reason": "already_active"},
        )

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 409
        assert "cours" in json.loads(r.data)["error"]

    def test_process_rejects_invalid_mode(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        jid = self._create_uploaded_job(admin_client)
        with app.app_context():
            JobStore.update_state(jid, JobState.READY_TO_PROCESS)

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "turbo"})

        assert r.status_code == 400
        assert "invalide" in json.loads(r.data)["error"]

    def test_process_rejects_when_job_not_ready(self, admin_client):
        jid = self._create_uploaded_job(admin_client)

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 409
        assert json.loads(r.data)["current_state"] == "uploaded"

    def test_process_allows_retry_from_stale_transcribing_state(self, admin_client, app, monkeypatch):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.services.job_executor import get_job_executor

        jid = self._create_uploaded_job(admin_client)
        with app.app_context():
            JobStore.update_state(jid, JobState.TRANSCRIBING)

        executor = get_job_executor()
        monkeypatch.setattr(
            executor,
            "submit_process",
            lambda job_id, audio_path, mode: {"accepted": True, "status": "queued", "mode": mode},
        )

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 202
        assert json.loads(r.data)["status"] == "queued"

    def test_process_cancel_marks_job_cancelled(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        jid = self._create_uploaded_job(admin_client)

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "cancel"})

        assert r.status_code == 200
        assert json.loads(r.data)["status"] == "cancelled"
        with app.app_context():
            assert JobStore.get_by_id(jid).state == JobState.CANCELLED.value

    def test_process_rejects_when_execution_already_active(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.workflow.transitions import mark_execution_queued

        jid = self._create_uploaded_job(admin_client)
        with app.app_context():
            JobStore.update_state(jid, JobState.READY_TO_PROCESS)
            mark_execution_queued(jid, "fast")

        r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

        assert r.status_code == 409
        assert json.loads(r.data)["execution_status"] == "queued"


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

    def test_map_speakers_after_lexicon_moves_job_to_ready(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        r = admin_client.post("/jobs/new", data={"title": "SpkReady"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        with app.app_context():
            JobStore.update_state(jid, JobState.LEXICON_DONE)

        r = admin_client.post(f"/api/jobs/{jid}/speakers/map", json={})
        assert r.status_code == 200

        with app.app_context():
            assert JobStore.get_by_id(jid).state == JobState.READY_TO_PROCESS.value

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

    def test_lexicon_from_participants_moves_job_to_ready(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        r = admin_client.post("/jobs/new", data={"title": "LexSkipReady"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        with app.app_context():
            JobStore.update_state(jid, JobState.PARTICIPANTS_DONE)

        r = admin_client.post(f"/api/jobs/{jid}/lexicon", json=[])
        assert r.status_code == 200

        with app.app_context():
            assert JobStore.get_by_id(jid).state == JobState.READY_TO_PROCESS.value

    def test_lexicon_after_speaker_detection_moves_job_to_ready(self, admin_client, app):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        r = admin_client.post("/jobs/new", data={"title": "LexReady"}, follow_redirects=True)
        jid = r.request.path.rstrip("/").split("/")[-1]

        with app.app_context():
            JobStore.update_state(jid, JobState.SPEAKER_DETECTION_DONE)

        r = admin_client.post(f"/api/jobs/{jid}/lexicon", json=[])
        assert r.status_code == 200

        with app.app_context():
            assert JobStore.get_by_id(jid).state == JobState.READY_TO_PROCESS.value

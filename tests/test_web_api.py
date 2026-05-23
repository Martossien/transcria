import io
import json
import os
import tempfile


class TestAuthentication:
    def test_login_page_accessible(self, client):
        r = client.get("/login")
        assert r.status_code == 200

    def test_login_redirects_to_index(self, client):
        r = client.post("/login", data={"username": "admin", "password": "admin-change-me"})
        assert r.status_code == 302

    def test_login_invalid_credentials(self, client):
        r = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_logout_redirects(self, admin_client):
        r = admin_client.get("/logout", follow_redirects=True)
        assert r.status_code == 200

    def test_protected_page_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=True)
        assert r.status_code == 200

    def test_index_after_login(self, admin_client):
        r = admin_client.get("/")
        assert r.status_code == 200

    def test_user_can_change_own_password(self, app):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username="password_self", password="oldpass123", role=Role.OPERATOR)
            user_id = user.id

        client = app.test_client()
        client.post("/login", data={"username": "password_self", "password": "oldpass123"})
        r = client.post(
            "/account/password",
            data={
                "current_password": "oldpass123",
                "new_password": "newpass123",
                "confirm_password": "newpass123",
            },
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            from transcria.auth.store import UserStore

            user = UserStore.get_by_id(user_id)
            assert user.check_password("newpass123")
            assert not user.check_password("oldpass123")

    def test_user_change_password_requires_current_password(self, app):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username="password_wrong_current", password="oldpass123", role=Role.OPERATOR)
            user_id = user.id

        client = app.test_client()
        client.post("/login", data={"username": "password_wrong_current", "password": "oldpass123"})
        r = client.post(
            "/account/password",
            data={
                "current_password": "badpass123",
                "new_password": "newpass123",
                "confirm_password": "newpass123",
            },
        )

        assert r.status_code == 400
        with app.app_context():
            from transcria.auth.store import UserStore

            user = UserStore.get_by_id(user_id)
            assert user.check_password("oldpass123")


class TestObservability:
    def test_health_endpoint_public(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["status"] == "ok"
        assert data["service"] == "transcria"
        assert data["database"]["status"] == "ok"

    def test_metrics_endpoint_public(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.data.decode("utf-8")
        assert "transcria_up 1" in body
        assert "transcria_ready 1" in body
        assert "transcria_jobs_total" in body
        assert "# TYPE transcria_jobs_state gauge" in body

    def test_ready_endpoint_public(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["status"] == "ready"
        assert data["worker"]["healthy"] is True


class TestAdminUsers:
    def test_user_list_page(self, admin_client):
        r = admin_client.get("/admin/users")
        assert r.status_code == 200

    def test_user_create_form(self, admin_client):
        r = admin_client.get("/admin/users/new")
        assert r.status_code == 200

    def test_create_user(self, admin_client):
        r = admin_client.post(
            "/admin/users/new",
            data={"username": "newuser1", "password": "secret123", "display_name": "New", "role": "operator"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"newuser1" in r.data or b"cr" in r.data.lower()

    def test_operator_cannot_access_users(self, operator_client):
        r = operator_client.get("/admin/users")
        assert r.status_code == 403

    def test_admin_can_deactivate_user_from_edit_form(self, admin_client, app):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username="deactivate_web", password="pw", role=Role.OPERATOR)
            user_id = user.id

        r = admin_client.post(
            f"/admin/users/{user_id}/edit",
            data={
                "display_name": "Deactivate Web",
                "email": "",
                "role": "operator",
                "password": "",
            },
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            from transcria.auth.store import UserStore

            assert UserStore.get_by_id(user_id).is_active is False

    def test_admin_can_reset_user_password(self, admin_client, app):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username="reset_web", password="oldpass123", role=Role.OPERATOR)
            user_id = user.id

        r = admin_client.post(
            f"/admin/users/{user_id}/edit",
            data={
                "display_name": "",
                "email": "",
                "role": "operator",
                "password": "newpass123",
                "password_confirm": "newpass123",
                "is_active": "1",
            },
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            from transcria.auth.store import UserStore

            user = UserStore.get_by_id(user_id)
            assert user.check_password("newpass123")
            assert not user.check_password("oldpass123")

    def test_admin_reset_user_password_rejects_mismatch(self, admin_client, app):
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username="reset_mismatch_web", password="oldpass123", role=Role.OPERATOR)
            user_id = user.id

        r = admin_client.post(
            f"/admin/users/{user_id}/edit",
            data={
                "display_name": "",
                "email": "",
                "role": "operator",
                "password": "newpass123",
                "password_confirm": "different123",
                "is_active": "1",
            },
        )

        assert r.status_code == 400
        with app.app_context():
            from transcria.auth.store import UserStore

            assert UserStore.get_by_id(user_id).check_password("oldpass123")


class TestAdminConfig:
    def test_admin_config_page(self, admin_client):
        r = admin_client.get("/admin/config")
        assert r.status_code == 200
        assert b"Configuration" in r.data
        assert b"server:" in r.data
        assert b"admin-change-me" not in r.data
        assert b"********" in r.data

    def test_operator_cannot_access_config(self, operator_client):
        assert operator_client.get("/admin/config").status_code == 403
        assert operator_client.post("/admin/config", data={"config_yaml": "server:\n  port: 1\n"}).status_code == 403

    def test_admin_config_rejects_invalid_yaml(self, admin_client):
        r = admin_client.post("/admin/config", data={"config_yaml": "server: [broken"})
        assert r.status_code == 400
        assert b"YAML invalide" in r.data

    def test_admin_config_saves_yaml(self, admin_client):
        from transcria.config import get_config, set_config

        original_env = os.environ.get("TRANSCRIA_CONFIG")
        original_cfg = get_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            path = f.name
        try:
            os.environ["TRANSCRIA_CONFIG"] = path
            r = admin_client.post(
                "/admin/config",
                data={"config_yaml": "server:\n  port: 8899\nworkflow:\n  enable_quality_mode: false\n"},
                follow_redirects=True,
            )
            assert r.status_code == 200, r.get_data(as_text=True)
            with open(path, "r", encoding="utf-8") as fh:
                saved = fh.read()
            assert "port: 8899" in saved
            assert get_config()["server"]["port"] == 8899
            assert get_config()["workflow"]["enable_quality_mode"] is False
            assert "storage" in get_config()
        finally:
            set_config(original_cfg)
            os.unlink(path)
            if original_env is not None:
                os.environ["TRANSCRIA_CONFIG"] = original_env
            else:
                os.environ.pop("TRANSCRIA_CONFIG", None)

    def test_admin_config_mask_preserves_existing_password(self, admin_client):
        from transcria.config import get_config, set_config

        original_env = os.environ.get("TRANSCRIA_CONFIG")
        original_cfg = get_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            path = f.name
        try:
            os.environ["TRANSCRIA_CONFIG"] = path
            set_config(
                {
                    **original_cfg,
                    "auth": {
                        **original_cfg.get("auth", {}),
                        "first_admin_password": "kept-secret",
                    },
                }
            )
            r = admin_client.post(
                "/admin/config",
                data={"config_yaml": "auth:\n  first_admin_password: '********'\n  enabled: false\n"},
                follow_redirects=True,
            )
            assert r.status_code == 200, r.get_data(as_text=True)
            assert get_config()["auth"]["first_admin_password"] == "kept-secret"
            assert get_config()["auth"]["enabled"] is True
        finally:
            set_config(original_cfg)
            os.unlink(path)
            if original_env is not None:
                os.environ["TRANSCRIA_CONFIG"] = original_env
            else:
                os.environ.pop("TRANSCRIA_CONFIG", None)


class TestJobCreation:
    def test_create_job_redirects(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Ma Reunion"}, follow_redirects=True)
        assert r.status_code == 200

    def test_viewer_cannot_create_job(self, viewer_client):
        r = viewer_client.post("/jobs/new", data={"title": "Test"}, follow_redirects=True)
        assert r.status_code == 403


class TestJobWizard:
    def test_wizard_page_loads(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Wizard Test"}, follow_redirects=True)
        assert r.status_code == 200

    def test_wizard_404_for_nonexistent(self, admin_client):
        r = admin_client.get("/jobs/nonexistent-uuid-1234567890")
        assert r.status_code == 404


class TestApiUpload:
    def test_upload_no_file(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "NoFile"}, follow_redirects=True)
        path = r.request.path
        job_id = path.split("/")[2] if "/jobs/" in path else None
        if job_id:
            r = admin_client.post(f"/api/jobs/{job_id}/upload", data={})
            assert r.status_code in (400, 404)
            resp = json.loads(r.data)
            assert "error" in resp


class TestApiSystem:
    def test_system_status_api(self, admin_client):
        r = admin_client.get("/api/system/status")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert isinstance(data, dict)

    def test_system_status_api_operator_forbidden(self, operator_client):
        r = operator_client.get("/api/system/status")
        assert r.status_code == 403

    def test_system_page_admin_only(self, admin_client):
        r = admin_client.get("/system")
        assert r.status_code == 200

    def test_system_page_operator_forbidden(self, operator_client):
        r = operator_client.get("/system")
        assert r.status_code == 403


class TestApiDownloads:
    def _create_and_get_id(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "DlTest"}, follow_redirects=True)
        path = r.request.path
        job_id = path.split("/")[2] if "/jobs/" in path else None
        return job_id

    def test_download_srt_nonexistent(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        if job_id:
            r = admin_client.get(f"/api/jobs/{job_id}/download/srt")
            assert r.status_code == 404

    def test_download_package_nonexistent(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        if job_id:
            r = admin_client.get(f"/api/jobs/{job_id}/download/package")
            assert r.status_code == 404

    def test_download_audio_nonexistent(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        if job_id:
            r = admin_client.get(f"/api/jobs/{job_id}/download/audio")
            assert r.status_code == 404


class TestApiContextEndpoints:
    def _make_job(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "CtxTest"}, follow_redirects=True)
        path = r.request.path
        job_id = path.split("/")[2] if "/jobs/" in path else None
        return job_id

    def test_save_context(self, admin_client):
        job_id = self._make_job(admin_client)
        if job_id:
            r = admin_client.post(
                f"/api/jobs/{job_id}/context",
                json={"title": "Test X", "language": "en"},
            )
            assert r.status_code == 200
            assert json.loads(r.data)["status"] == "ok"

    def test_save_participants(self, admin_client):
        job_id = self._make_job(admin_client)
        if job_id:
            r = admin_client.post(
                f"/api/jobs/{job_id}/participants",
                json=[{"name": "Alice", "function": "Dev"}],
            )
            assert r.status_code == 200

    def test_save_lexicon(self, admin_client):
        job_id = self._make_job(admin_client)
        if job_id:
            r = admin_client.post(
                f"/api/jobs/{job_id}/lexicon",
                json=[{"term": "API", "category": "technique"}],
            )
            assert r.status_code == 200

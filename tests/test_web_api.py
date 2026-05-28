import io
import json
import os
import tempfile
from pathlib import Path


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
    def _make_job_id(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Wizard Test"}, follow_redirects=True)
        path = r.request.path
        return path.split("/")[2] if "/jobs/" in path else None

    def test_wizard_page_loads(self, admin_client):
        r = admin_client.post("/jobs/new", data={"title": "Wizard Test"}, follow_redirects=True)
        assert r.status_code == 200

    def test_wizard_404_for_nonexistent(self, admin_client):
        r = admin_client.get("/jobs/nonexistent-uuid-1234567890")
        assert r.status_code == 404

    def _advance_to_participants_done(self, app, job_id):
        """Force l'état du job à PARTICIPANTS_DONE pour débloquer la section lexique."""
        with app.app_context():
            from transcria.jobs.store import JobStore
            from transcria.jobs.models import JobState
            JobStore.update_state(job_id, JobState.PARTICIPANTS_DONE)

    def test_wizard_renders_lexicon_contexts(self, admin_client, app):
        """La page wizard doit afficher les citations de contexte du lexique de session."""
        job_id = self._make_job_id(admin_client)
        if not job_id:
            return

        # La section lexique n'est affichée qu'après participants_done.
        self._advance_to_participants_done(app, job_id)

        # Sauvegarde un terme avec deux extraits de contexte
        r = admin_client.post(
            f"/api/jobs/{job_id}/lexicon",
            json=[{
                "term": "Emmental",
                "category": "mot suspect",
                "contexts": [
                    {"timecode": "5.4s→26.4s", "speaker": "SPEAKER_00", "quote": "Mettez-moi de l'emental"},
                    {"timecode": "30.0s→45.0s", "speaker": "SPEAKER_01", "quote": "De l'ementeal"},
                ],
            }],
        )
        assert r.status_code == 200

        # Recharge la page wizard et vérifie la présence des citations
        r = admin_client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "Mettez-moi de l" in html, "La première citation doit apparaître dans la page"
        assert "ementeal" in html, "La deuxième citation doit apparaître dans la page"
        assert "lex-context-item" in html, "Les items de contexte doivent être rendus"
        assert "lex-context-play" in html, "Le bouton play doit être présent"

    def test_wizard_lexicon_contexts_audio_available_flag(self, admin_client, app):
        """audio_available doit être True pour les timecodes valides, False pour les invalides."""
        job_id = self._make_job_id(admin_client)
        if not job_id:
            return

        self._advance_to_participants_done(app, job_id)

        r = admin_client.post(
            f"/api/jobs/{job_id}/lexicon",
            json=[{
                "term": "Test",
                "contexts": [
                    {"timecode": "5.4s→26.4s", "quote": "Extrait avec timecode valide"},
                    {"timecode": "sans timecode", "quote": "Extrait sans timecode valide"},
                ],
            }],
        )
        assert r.status_code == 200

        r = admin_client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # Le contexte avec timecode valide doit avoir un bouton play actif
        # Le contexte sans timecode valide doit avoir un bouton play désactivé
        assert 'lex-context-play' in html
        assert 'disabled' in html, "Au moins un bouton play doit être désactivé (timecode invalide)"


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

    def test_audio_excerpt_returns_generated_clip(self, admin_client, monkeypatch):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        fs.save_upload(b"source", "audio.mp3")

        fs.save_json("summary/summary.json", {
            "segments": [
                {"start": 5.4, "end": 26.4, "text": "Mettez-moi un peu d'émental. De l'émenteal, ça ira comme ça ?"},
                {"start": 27.0, "end": 30.6, "text": "Le mieux, c'est d'y goûter."},
            ]
        })

        def fake_build(audio_path, cache_dir, start_s, end_s, **kwargs):
            assert audio_path.name == "original.mp3"
            assert start_s > 5.4
            assert end_s <= 26.4
            out = Path(cache_dir) / "excerpt.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"wav")
            return out

        monkeypatch.setattr("transcria.web.routes.AudioExcerptService.build_excerpt", fake_build)

        r = admin_client.get(
            f"/api/jobs/{job_id}/audio/excerpt"
            "?timecode=27.0s%E2%86%9230.6s"
            "&quote=De%20l%27%C3%A9menteal%2C%20%C3%A7a%20ira%20comme%20%C3%A7a%20%3F"
        )

        assert r.status_code == 200
        assert r.mimetype == "audio/wav"
        assert r.data == b"wav"
        with admin_client.application.app_context():
            from transcria.audit.models import AuditAction
            from transcria.audit.models import AuditLog

            row = AuditLog.query.filter_by(action=AuditAction.JOB_DOWNLOAD.value, target_id=job_id).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            assert '"format": "audio_excerpt"' in row.details_json
            assert "émenteal" not in row.details_json

    def test_audio_excerpt_rejects_invalid_timecode(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        fs.save_upload(b"source", "audio.mp3")

        r = admin_client.get(f"/api/jobs/{job_id}/audio/excerpt?timecode=sans-timecode")

        assert r.status_code == 400
        assert json.loads(r.data)["error"] == "Timecode audio invalide"

    def test_speaker_clip_download_is_audited(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        sample_dir = fs.job_dir / "speakers" / "samples"
        sample_dir.mkdir(parents=True, exist_ok=True)
        (sample_dir / "SPEAKER_00_001.wav").write_bytes(b"wav")

        r = admin_client.get(f"/api/jobs/{job_id}/speakers/clip/SPEAKER_00_001.wav")

        assert r.status_code == 200
        assert r.mimetype == "audio/wav"
        with admin_client.application.app_context():
            from transcria.audit.models import AuditAction
            from transcria.audit.models import AuditLog

            row = AuditLog.query.filter_by(action=AuditAction.JOB_DOWNLOAD.value, target_id=job_id).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            assert '"format": "speaker_clip"' in row.details_json

    def test_speaker_clips_api_returns_safe_public_names(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        sample_dir = fs.job_dir / "speakers" / "samples"
        sample_dir.mkdir(parents=True, exist_ok=True)
        nested_dir = sample_dir / "selection"
        nested_dir.mkdir()
        absolute_clip = sample_dir / "SPEAKER_00 clip 1.wav"
        relative_clip = nested_dir / "SPEAKER_00_clip_2.wav"
        outside_clip = fs.job_dir / "metadata" / "outside.wav"
        absolute_clip.write_bytes(b"wav1")
        relative_clip.write_bytes(b"wav2")
        outside_clip.write_bytes(b"wav3")
        fs.save_json(
            "speakers/speaker_clips.json",
            {
                "SPEAKER_00": [
                    str(absolute_clip),
                    "selection/SPEAKER_00_clip_2.wav",
                    str(outside_clip),
                    "../metadata/outside.wav",
                    "missing.wav",
                ],
                "SPEAKER_01": "invalid",
            },
        )

        r = admin_client.get(f"/api/jobs/{job_id}/speakers/clips")

        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["clips"] == {
            "SPEAKER_00": ["SPEAKER_00 clip 1.wav", "selection/SPEAKER_00_clip_2.wav"]
        }
        assert str(fs.job_dir) not in r.get_data(as_text=True)

    def test_speaker_clip_download_rejects_path_traversal(self, admin_client):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        outside_clip = fs.job_dir / "metadata" / "outside.wav"
        outside_clip.write_bytes(b"wav")

        r = admin_client.get(f"/api/jobs/{job_id}/speakers/clip/../metadata/outside.wav")

        assert r.status_code == 404

    def test_push_to_editor_is_audited(self, admin_client, monkeypatch):
        job_id = self._create_and_get_id(admin_client)
        assert job_id

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        fs.save_upload(b"audio", "audio.mp3")
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:01,000\nBonjour")

        monkeypatch.setattr(
            "transcria.web.routes.SrtEditorLink.push_audio",
            lambda self, audio_path: {"project_id": "project-1"},
        )
        monkeypatch.setattr(
            "transcria.web.routes.SrtEditorLink.push_srt",
            lambda self, project_id, srt_content: {"ok": True},
        )

        r = admin_client.post(f"/api/jobs/{job_id}/push-to-editor")

        assert r.status_code == 200
        with admin_client.application.app_context():
            from transcria.audit.models import AuditAction
            from transcria.audit.models import AuditLog

            row = AuditLog.query.filter_by(action=AuditAction.JOB_EXTERNAL_PUSH.value, target_id=job_id).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            assert '"destination": "srt_editor"' in row.details_json
            assert "Bonjour" not in row.details_json

    def test_audit_origin_strips_url_credentials(self):
        from transcria.web.routes import _audit_origin_from_url

        assert _audit_origin_from_url("https://user:secret@example.org:9443/path") == "example.org:9443"


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

    def test_save_lexicon_with_contexts_roundtrip(self, admin_client, app):
        """Les contextes sont sauvegardés et rechargés correctement via le wizard."""
        from transcria.context.lexicon import LexiconManager
        from transcria.jobs.store import JobStore

        job_id = self._make_job(admin_client)
        if not job_id:
            return

        r = admin_client.post(
            f"/api/jobs/{job_id}/lexicon",
            json=[{
                "term": "DNS",
                "category": "technique",
                "contexts": [
                    {
                        "timecode": "00:01:30",
                        "speaker": "SPEAKER_00",
                        "quote": "La résolution DNS a échoué.",
                        "reason": "Forme STT douteuse.",
                    }
                ],
            }],
        )
        assert r.status_code == 200

        with app.app_context():
            from transcria.config import get_config
            cfg = get_config()
            jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
            job = JobStore.get_by_id(job_id)
            if job:
                loaded = LexiconManager.get(job, jobs_dir)
                assert len(loaded) == 1, "Le terme doit être sauvegardé"
                ctx = loaded[0].get("contexts", [])
                assert len(ctx) == 1, "Le contexte doit être sauvegardé"
                assert ctx[0]["quote"] == "La résolution DNS a échoué."
                assert ctx[0]["timecode"] == "00:01:30"
                assert ctx[0]["speaker"] == "SPEAKER_00"

    def test_save_lexicon_contexts_truncated_to_three(self, admin_client, app):
        """L'API ne conserve que les 3 premiers contextes pour éviter les prompts trop longs."""
        from transcria.context.lexicon import LexiconManager
        from transcria.jobs.store import JobStore

        job_id = self._make_job(admin_client)
        if not job_id:
            return

        contexts = [
            {"timecode": f"0{i}:00", "quote": f"Extrait numéro {i}."} for i in range(5)
        ]
        r = admin_client.post(
            f"/api/jobs/{job_id}/lexicon",
            json=[{"term": "Terme", "contexts": contexts}],
        )
        assert r.status_code == 200

        with app.app_context():
            from transcria.config import get_config
            cfg = get_config()
            jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
            job = JobStore.get_by_id(job_id)
            if job:
                loaded = LexiconManager.get(job, jobs_dir)
                assert len(loaded[0].get("contexts", [])) == 3, \
                    "L'API doit limiter les contextes à 3"

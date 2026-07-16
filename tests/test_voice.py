import io
import uuid

import numpy as np

from transcria.config import load_config
from transcria.config.config_schema import validate_config
from transcria.voice.embedding import cosine_raw, deserialize_embedding, serialize_embedding
from transcria.voice.models import VoiceConsent, VoiceConsentStatus, VoiceMatch, VoiceProfile, VoiceProfileStatus, VoiceSubject


class TestVoiceConfig:
    def test_default_voice_enrollment_config_is_valid(self):
        cfg = load_config("/tmp/transcria-test-missing-config.yaml")

        result = validate_config(cfg)

        assert result.is_valid
        assert cfg["voice_enrollment"]["enabled"] is False
        assert cfg["voice_enrollment"]["storage_dir"] == "./voices"

    def test_rejects_invalid_voice_thresholds(self):
        cfg = load_config()
        cfg["voice_enrollment"]["matching"]["suggestion_threshold"] = 0.9
        cfg["voice_enrollment"]["matching"]["high_confidence_threshold"] = 0.8

        result = validate_config(cfg)

        assert not result.is_valid
        assert any("suggestion_threshold" in msg for msg in result.errors)


class TestVoiceEmbeddingUtils:
    def test_serializes_normalized_embedding(self):
        vector = np.array([3.0, 4.0], dtype=np.float32)

        blob = serialize_embedding(vector)
        restored = deserialize_embedding(blob, 2)

        assert np.allclose(restored, np.array([0.6, 0.8], dtype=np.float32))
        assert cosine_raw(restored, restored) == 1.0


class TestVoiceStore:
    def test_create_subject_requires_group_when_global_disabled(self, app):
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.voice.store import VoiceStore, VoiceValidationError

            admin = UserStore.get_by_username("admin")
            try:
                VoiceStore.create_subject(
                    actor=admin,
                    display_name="Sans groupe",
                    group_id=None,
                    allow_global_profiles=False,
                )
            except VoiceValidationError as exc:
                assert "groupe" in str(exc)
            else:
                raise AssertionError("VoiceValidationError attendu")

    def test_create_subject_with_group_and_consent(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.voice.store import VoiceStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(f"voices-{uuid.uuid4().hex[:8]}")
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Alice Voice",
                group_id=group.id,
                gender="female",
                allow_global_profiles=False,
            )
            consent = VoiceStore.create_consent(
                subject=subject,
                actor=admin,
                form_version="voice-consent-v1",
                status=VoiceConsentStatus.ACTIVE,
                proof_path="/tmp/proof.pdf",
                proof_sha256="a" * 64,
            )

            assert subject.id
            assert subject.gender == "female"
            assert consent.status == VoiceConsentStatus.ACTIVE.value
            assert VoiceStore.active_consent(subject).id == consent.id

    def test_matchable_profiles_use_job_owner_group_scope(self, app, owner_id):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.jobs.store import JobStore
            from transcria.voice.embedding import VoiceEmbedding
            from transcria.voice.store import VoiceStore

            admin = UserStore.get_by_username("admin")
            owner = UserStore.get_by_id(owner_id)
            group = GroupStore.create_group(f"voice-scope-{uuid.uuid4().hex[:8]}")
            GroupStore.add_member(group.id, owner.id)
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Scope Voice",
                group_id=group.id,
                allow_global_profiles=False,
            )
            consent = VoiceStore.create_consent(
                subject=subject,
                actor=admin,
                form_version="voice-consent-v1",
                status=VoiceConsentStatus.ACTIVE,
                proof_path="/tmp/proof.pdf",
                proof_sha256="c" * 64,
            )
            profile = VoiceStore.create_processing_profile(
                subject,
                consent,
                admin,
                get_config()["voice_enrollment"]["embedding"],
            )
            VoiceStore.complete_profile(
                profile,
                VoiceEmbedding(
                    vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                    backend="pyannote",
                    model_id=get_config()["voice_enrollment"]["embedding"]["model_id"],
                    model_revision="",
                    normalization="l2",
                    sample_count=1,
                    speech_duration_s=8.0,
                ),
                admin,
            )
            job = JobStore.create_job(owner_id=owner.id, title="voice scope")

            profiles, scope = VoiceStore.matchable_profiles_for_job(job, get_config())

            assert scope["group_ids"] == [group.id]
            assert [p.subject_id for p in profiles] == [subject.id]

    def test_matchable_profiles_for_admin_job_include_all_groups(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.jobs.store import JobStore
            from transcria.voice.embedding import VoiceEmbedding
            from transcria.voice.store import VoiceStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(f"voice-admin-scope-{uuid.uuid4().hex[:8]}")
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Admin Scope Voice",
                group_id=group.id,
                allow_global_profiles=False,
            )
            consent = VoiceStore.create_consent(
                subject=subject,
                actor=admin,
                form_version="voice-consent-v1",
                status=VoiceConsentStatus.ACTIVE,
                proof_path="/tmp/proof.pdf",
                proof_sha256="e" * 64,
            )
            profile = VoiceStore.create_processing_profile(subject, consent, admin, get_config()["voice_enrollment"]["embedding"])
            VoiceStore.complete_profile(
                profile,
                VoiceEmbedding(
                    vector=np.array([0.0, 1.0], dtype=np.float32),
                    backend="pyannote",
                    model_id=get_config()["voice_enrollment"]["embedding"]["model_id"],
                    model_revision="",
                    normalization="l2",
                    sample_count=1,
                    speech_duration_s=8.0,
                ),
                admin,
            )
            job = JobStore.create_job(owner_id=admin.id, title="admin voice scope")

            profiles, scope = VoiceStore.matchable_profiles_for_job(job, get_config())

            assert scope["scope"] == "admin_all"
            assert subject.id in {p.subject_id for p in profiles}


class TestVoiceWeb:
    def test_operator_cannot_access_voice_admin(self, operator_client):
        assert operator_client.get("/admin/voices").status_code == 403

    def test_admin_can_download_blank_consent_pdf(self, admin_client):
        response = admin_client.get("/admin/voices/consent-form.pdf")

        assert response.status_code == 200
        assert response.content_type == "application/pdf"
        assert response.data.startswith(b"%PDF-")
        assert b"voice-consent-v1" in response.data
        assert b"Consentement pour empreinte vocale" in response.data  # fr par défaut

    def test_consent_pdf_follows_interface_language(self, admin_client):
        """Le formulaire vierge suit la langue de l'interface (axe A). Locale via session
        pour ne pas persister user.locale de l'admin partagé (cf. mémoire i18n)."""
        with admin_client.session_transaction() as sess:
            sess["ui_locale"] = "en"
        try:
            response = admin_client.get("/admin/voices/consent-form.pdf")
            assert response.status_code == 200
            assert b"Voice fingerprint consent" in response.data
            assert b"Consentement" not in response.data  # zéro FR en EN
            assert b"voice-consent-v1" in response.data  # form_version inchangé
            assert "voice_fingerprint_consent_v1.pdf" in response.headers["Content-Disposition"]
        finally:
            with admin_client.session_transaction() as sess:
                sess.pop("ui_locale", None)

    def test_admin_can_create_voice_and_upload_consent(self, app, admin_client):
        group_name = f"voice-web-{uuid.uuid4().hex[:8]}"
        with app.app_context():
            from transcria.auth.groups import GroupStore

            group = GroupStore.create_group(group_name)
            group_id = group.id

        r = admin_client.post(
            "/admin/voices/new",
            data={"display_name": "Bob Voice", "group_id": group_id, "gender": "male"},
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            subject = VoiceSubject.query.filter_by(display_name="Bob Voice").one()
            subject_id = subject.id
            assert subject.gender == "male"

        r = admin_client.post(
            f"/admin/voices/{subject_id}/consents",
            data={
                "status": "active",
                "proof": (io.BytesIO(b"preuve signee"), "consent.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            consent = VoiceConsent.query.filter_by(subject_id=subject_id).one()
            consent_id = consent.id
            assert consent.status == VoiceConsentStatus.ACTIVE.value
            assert consent.proof_sha256

        proof = admin_client.get(f"/admin/voices/{subject_id}/consent-proof/{consent_id}")
        assert proof.status_code == 200
        assert proof.data == b"preuve signee"

    def test_admin_can_update_voice_metadata(self, app, admin_client):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.voice.store import VoiceStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(f"voice-edit-{uuid.uuid4().hex[:8]}")
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Nom initial",
                gender="female",
                group_id=group.id,
                allow_global_profiles=False,
            )
            subject_id = subject.id

        response = admin_client.post(
            f"/admin/voices/{subject_id}/metadata",
            data={
                "display_name": "martossien",
                "gender": "male",
                "email": "martossien@example.test",
                "external_ref": "informatique",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        with app.app_context():
            from transcria.database import db

            subject = db.session.get(VoiceSubject, subject_id)
            assert subject.display_name == "martossien"
            assert subject.gender == "male"
            assert subject.email == "martossien@example.test"
            assert subject.external_ref == "informatique"

    def test_generate_profile_route_uses_embedding_service(self, app, admin_client, monkeypatch):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.voice.store import VoiceStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(f"voice-gen-{uuid.uuid4().hex[:8]}")
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Claire Voice",
                group_id=group.id,
                allow_global_profiles=False,
            )
            VoiceStore.create_consent(
                subject=subject,
                actor=admin,
                form_version="voice-consent-v1",
                status=VoiceConsentStatus.ACTIVE,
                proof_path="/tmp/proof.pdf",
                proof_sha256="b" * 64,
            )

        def fake_extract(self, audio_path):
            from transcria.voice.embedding import VoiceEmbedding

            return VoiceEmbedding(
                vector=np.array([1.0, 0.0], dtype=np.float32),
                backend="pyannote",
                model_id="test-model",
                model_revision="",
                normalization="l2",
                sample_count=1,
                speech_duration_s=12.0,
            )

        monkeypatch.setattr("transcria.voice.embedding.PyannoteVoiceEmbeddingBackend.extract_reference_embedding", fake_extract)

        r = admin_client.post(
            f"/admin/voices/{subject.id}/generate",
            data={"audio": (io.BytesIO(b"fake wav"), "voice.wav")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert r.status_code == 200
        with app.app_context():
            profile = VoiceProfile.query.filter_by(subject_id=subject.id).one()
            assert profile.status == VoiceProfileStatus.ACTIVE.value
            assert profile.embedding_dim == 2
            assert profile.embedding_blob is not None

    def test_voice_match_route_suggests_known_voice(self, app, admin_client, monkeypatch):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.voice.embedding import VoiceEmbedding
            from transcria.voice.store import VoiceStore

            cfg = get_config()
            cfg["voice_enrollment"]["enabled"] = True
            cfg["voice_enrollment"]["require_explicit_job_group_for_multi_group_users"] = False
            admin = UserStore.get_by_username("admin")
            owner = UserStore.create_user(
                username=f"voice_match_owner_{uuid.uuid4().hex[:8]}",
                password="test12345",
                role=Role.OPERATOR,
            )
            group = GroupStore.create_group(f"voice-match-{uuid.uuid4().hex[:8]}")
            GroupStore.add_member(group.id, owner.id)
            subject = VoiceStore.create_subject(
                actor=admin,
                display_name="Diane Voice",
                group_id=group.id,
                gender="female",
                allow_global_profiles=False,
            )
            consent = VoiceStore.create_consent(
                subject=subject,
                actor=admin,
                form_version="voice-consent-v1",
                status=VoiceConsentStatus.ACTIVE,
                proof_path="/tmp/proof.pdf",
                proof_sha256="d" * 64,
            )
            profile = VoiceStore.create_processing_profile(subject, consent, admin, cfg["voice_enrollment"]["embedding"])
            VoiceStore.complete_profile(
                profile,
                VoiceEmbedding(
                    vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                    backend="pyannote",
                    model_id=cfg["voice_enrollment"]["embedding"]["model_id"],
                    model_revision="",
                    normalization="l2",
                    sample_count=1,
                    speech_duration_s=10.0,
                ),
                admin,
            )
            job = JobStore.create_job(owner_id=owner.id, title="voice match")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            clip_path = fs.job_dir / "speakers" / "samples" / "SPEAKER_00_clip1.wav"
            clip_path.write_bytes(b"fake wav")
            fs.save_json("speakers/speaker_clips.json", {"SPEAKER_00": [str(clip_path)]})

        def fake_extract(self, audio_path):
            from transcria.voice.embedding import VoiceEmbedding

            return VoiceEmbedding(
                vector=np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
                backend="pyannote",
                model_id=self.model_id,
                model_revision=self.model_revision,
                normalization="l2",
                sample_count=1,
                speech_duration_s=4.0,
            )

        monkeypatch.setattr("transcria.voice.embedding.PyannoteVoiceEmbeddingBackend.extract_reference_embedding", fake_extract)

        response = admin_client.post(f"/api/jobs/{job.id}/speakers/voice-match")

        assert response.status_code == 200
        data = response.get_json()
        assert data["matches"][0]["suggested_name"] == "Diane Voice"
        assert data["matches"][0]["suggested_gender"] == "female"
        with app.app_context():
            assert VoiceMatch.query.filter_by(job_id=job.id, speaker_id="SPEAKER_00").count() == 1


class TestVoiceMatchingRobustness:
    def test_speaker_with_degenerate_mean_is_skipped_not_crash(self, tmp_path, monkeypatch):
        # Chasse aux bugs : deux extraits ~opposés pour le MÊME locuteur → moyenne nulle
        # → normalize_l2 levait `embedding_norme_nulle` HORS du try (ligne 131) → tout le
        # matching crashait. Le locuteur dégénéré doit être ignoré, pas faire échouer le job.
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.voice.embedding import PyannoteVoiceEmbeddingBackend, VoiceEmbedding
        from transcria.voice.matching import VoiceMatchingService

        fs = JobFilesystem(str(tmp_path), "job-voice-deg")
        (fs.job_dir / "speakers").mkdir(parents=True, exist_ok=True)
        c1 = fs.job_dir / "speakers" / "c1.wav"; c1.write_bytes(b"x")
        c2 = fs.job_dir / "speakers" / "c2.wav"; c2.write_bytes(b"x")
        fs.save_json("speakers/speaker_clips.json", {"SPEAKER_00": [str(c1), str(c2)]})

        vecs = iter([
            np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        ])

        def fake_extract(self, clip_path):
            return VoiceEmbedding(
                vector=next(vecs), backend="pyannote", model_id="m", model_revision="",
                normalization="l2", sample_count=1, speech_duration_s=1.0,
            )
        monkeypatch.setattr(PyannoteVoiceEmbeddingBackend, "extract_reference_embedding", fake_extract)

        svc = VoiceMatchingService({"storage": {"jobs_dir": str(tmp_path)}}, device="cpu")
        embeddings = svc._speaker_embeddings_from_clips(fs)
        assert "SPEAKER_00" not in embeddings


class TestVoiceEnrollmentCleanup:
    def test_failed_embedding_deletes_orphan_source_audio(self, tmp_path, monkeypatch):
        # Chasse aux bugs : sur échec d'embedding, l'audio source consenti restait sur
        # disque (fichier orphelin non tracé) — contraire à « supprimé par défaut ».
        import pytest

        from transcria.voice import enrollment as enroll_mod
        from transcria.voice.embedding import VoiceEmbeddingError
        from transcria.voice.enrollment import VoiceEnrollmentService

        audio = tmp_path / "ref.wav"
        audio.write_bytes(b"x")

        monkeypatch.setattr(enroll_mod.VoiceStore, "active_consent", staticmethod(lambda subject: object()))
        monkeypatch.setattr(enroll_mod.VoiceStore, "create_processing_profile", staticmethod(lambda *a, **k: object()))
        monkeypatch.setattr(enroll_mod.VoiceStore, "fail_profile", staticmethod(lambda *a, **k: None))

        class _RaisingBackend:
            def extract_reference_embedding(self, path):
                raise VoiceEmbeddingError("boom")

        monkeypatch.setattr(enroll_mod, "create_voice_embedding_backend", lambda config, device="cpu": _RaisingBackend())

        svc = VoiceEnrollmentService({"voice_enrollment": {"delete_source_audio_after_embedding": True}})
        with pytest.raises(VoiceEmbeddingError):
            svc.generate_profile(object(), object(), audio)
        assert not audio.exists()

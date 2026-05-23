import io
import uuid

import numpy as np

from transcria.voice.models import VoiceMatch
from transcria.voice.models import VoiceProfile
from transcria.voice.models import VoiceProfileStatus
from transcria.voice.models import VoiceSubject


class TestVoiceEnrollmentE2E:
    def test_admin_flow_enrolls_voice_and_suggests_it_for_job(self, app, admin_client, monkeypatch):
        """Parcours E2E applicatif voix enregistrées, sans modèle GPU réel."""
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.database import db
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.models import JobState
            from transcria.jobs.store import JobStore

            cfg = get_config()
            cfg["voice_enrollment"]["enabled"] = True
            cfg["voice_enrollment"]["require_explicit_job_group_for_multi_group_users"] = False
            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(f"voice-e2e-{uuid.uuid4().hex[:8]}")
            GroupStore.add_member(group.id, admin.id)
            group_id = group.id
            job = JobStore.create_job(owner_id=admin.id, title="E2E voix enregistrées")
            job.state = JobState.CONTEXT_DONE.value
            db.session.commit()
            job_id = job.id
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            clip_path = fs.job_dir / "speakers" / "samples" / "SPEAKER_00_clip1.wav"
            clip_path.write_bytes(b"fake speaker clip")
            fs.save_json("speakers/speaker_clips.json", {"SPEAKER_00": [str(clip_path)]})
            fs.save_json("speakers/speaker_stats.json", {
                "speakers": [{
                    "speaker_id": "SPEAKER_00",
                    "speaking_time_seconds": 42,
                    "turn_count": 3,
                    "gender": "female",
                }],
            })

        def fake_extract(self, audio_path):
            from transcria.voice.embedding import VoiceEmbedding

            return VoiceEmbedding(
                vector=np.array([1.0, 0.0, 0.0], dtype=np.float32),
                backend="pyannote",
                model_id=self.model_id,
                model_revision=self.model_revision,
                normalization="l2",
                sample_count=1,
                speech_duration_s=8.0,
            )

        monkeypatch.setattr(
            "transcria.voice.embedding.PyannoteVoiceEmbeddingBackend.extract_reference_embedding",
            fake_extract,
        )

        pdf = admin_client.get("/admin/voices/consent-form.pdf")
        assert pdf.status_code == 200
        assert pdf.data.startswith(b"%PDF-")

        response = admin_client.post(
            "/admin/voices/new",
            data={"display_name": "Alice Martin", "group_id": group_id, "email": "alice@example.test"},
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            subject = VoiceSubject.query.filter_by(display_name="Alice Martin").one()
            subject_id = subject.id

        response = admin_client.post(
            f"/admin/voices/{subject_id}/consents",
            data={"status": "active", "proof": (io.BytesIO(b"preuve signee"), "consent.pdf")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200

        response = admin_client.post(
            f"/admin/voices/{subject_id}/generate",
            data={"audio": (io.BytesIO(b"audio reference"), "alice.wav")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200

        response = admin_client.post(f"/api/jobs/{job_id}/speakers/voice-match")
        assert response.status_code == 200
        data = response.get_json()
        assert data["available"] is True
        assert data["matches"][0]["speaker_id"] == "SPEAKER_00"
        assert data["matches"][0]["suggested_name"] == "Alice Martin"

        page = admin_client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Alice Martin" in page.get_data(as_text=True)
        assert "Rechercher les voix connues" in page.get_data(as_text=True)

        with app.app_context():
            profile = VoiceProfile.query.filter_by(subject_id=subject_id).one()
            assert profile.status == VoiceProfileStatus.ACTIVE.value
            assert VoiceMatch.query.filter_by(job_id=job_id, speaker_id="SPEAKER_00").count() == 1
            voice_matches = fs.load_json("speakers/voice_matches.json")
            assert voice_matches["matches"][0]["suggested_name"] == "Alice Martin"

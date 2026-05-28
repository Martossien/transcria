"""Tests JobService — couverture des 5 méthodes publiques et des helpers privés."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transcria.jobs.models import JobState
from transcria.services.job_service import (
    JobService,
    _merge_speakers_with_participants,
    _quality_summary_from_preflight,
)


# ---------------------------------------------------------------------------
# _quality_summary_from_preflight (conservés depuis l'ancienne version)
# ---------------------------------------------------------------------------

def test_quality_summary_from_preflight_exposes_risk_level():
    summary = _quality_summary_from_preflight({"risk_level": "degrade"})
    assert summary == {"diagnostics": {"level": "degrade"}}


def test_quality_summary_from_preflight_ignores_missing_level():
    assert _quality_summary_from_preflight({}) == {}
    assert _quality_summary_from_preflight({"flags": ["audio_faible"]}) == {}


# ---------------------------------------------------------------------------
# _merge_speakers_with_participants
# ---------------------------------------------------------------------------

class TestMergeSpeakersWithParticipants:
    def test_empty_mapping_returns_empty(self):
        assert _merge_speakers_with_participants({}, []) == []

    def test_none_mapping_returns_empty(self):
        assert _merge_speakers_with_participants(None, []) == []

    def test_string_value_sets_name(self):
        result = _merge_speakers_with_participants({"spk_0": "Alice"}, [])
        assert result == [{"speaker_id": "spk_0", "name": "Alice", "participant_id": ""}]

    def test_dict_value_without_participant_id(self):
        result = _merge_speakers_with_participants({"spk_0": {"name": "Bob"}}, [])
        assert result == [{"speaker_id": "spk_0", "name": "Bob", "participant_id": ""}]

    def test_dict_value_with_matched_participant(self):
        mapping = {"spk_0": {"name": "Carol", "participant_id": "p1"}}
        participants = [{"id": "p1", "name": "Carol Dupont"}]
        result = _merge_speakers_with_participants(mapping, participants)
        assert len(result) == 1
        assert result[0]["participant"] == {"id": "p1", "name": "Carol Dupont"}
        assert result[0]["participant_id"] == "p1"

    def test_dict_value_with_unmatched_participant_id(self):
        mapping = {"spk_0": {"name": "Dave", "participant_id": "p-missing"}}
        result = _merge_speakers_with_participants(mapping, [])
        assert "participant" not in result[0]

    def test_multiple_speakers_all_present(self):
        mapping = {"spk_0": "Alice", "spk_1": "Bob"}
        result = _merge_speakers_with_participants(mapping, [])
        ids = {r["speaker_id"] for r in result}
        assert ids == {"spk_0", "spk_1"}

    def test_speaker_id_used_as_name_fallback_for_unknown_type(self):
        result = _merge_speakers_with_participants({"spk_0": 42}, [])
        assert result[0]["name"] == "spk_0"


# ---------------------------------------------------------------------------
# JobService.create
# ---------------------------------------------------------------------------

class TestJobServiceCreate:
    def test_returns_job_id_title_state(self, app, owner_id):
        with app.app_context():
            result = JobService.create(owner_id, "Réunion test")
        assert "job_id" in result
        assert result["title"] == "Réunion test"
        assert result["state"] == JobState.CREATED

    def test_job_id_is_non_empty_string(self, app, owner_id):
        with app.app_context():
            result = JobService.create(owner_id, "Titre")
        assert isinstance(result["job_id"], str)
        assert len(result["job_id"]) > 0

    def test_distinct_jobs_have_different_ids(self, app, owner_id):
        with app.app_context():
            r1 = JobService.create(owner_id, "A")
            r2 = JobService.create(owner_id, "B")
        assert r1["job_id"] != r2["job_id"]


# ---------------------------------------------------------------------------
# JobService.upload
# ---------------------------------------------------------------------------

class TestJobServiceUpload:
    def test_unknown_job_returns_error(self, app):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                result = JobService.upload("nonexistent-id", b"data", "test.wav", d)
        assert "error" in result

    def test_sets_state_to_uploaded(self, app, owner_id):
        from transcria.jobs.store import JobStore

        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "Réunion sans titre")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.save_upload.return_value = {"size_bytes": 99}
                    JobService.upload(job_id, b"audio", "meeting.wav", d)

                job = JobStore.get_by_id(job_id)
                assert job.state == JobState.UPLOADED.value

    def test_updates_default_title_from_filename_stem(self, app, owner_id):
        from transcria.jobs.store import JobStore

        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "Réunion sans titre")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.save_upload.return_value = {}
                    JobService.upload(job_id, b"", "budget_q4.mp3", d)

                job = JobStore.get_by_id(job_id)
                assert job.title == "budget_q4"

    def test_keeps_non_default_title(self, app, owner_id):
        from transcria.jobs.store import JobStore

        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "Mon titre perso")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.save_upload.return_value = {}
                    JobService.upload(job_id, b"", "ignored.wav", d)

                job = JobStore.get_by_id(job_id)
                assert job.title == "Mon titre perso"

    def test_returns_filesystem_metadata(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "upload meta test")
                job_id = info["job_id"]

                expected = {"size_bytes": 42, "format": "wav", "mime_type": "audio/wav"}
                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.save_upload.return_value = expected
                    result = JobService.upload(job_id, b"", "x.wav", d)

        assert result == expected


# ---------------------------------------------------------------------------
# JobService.analyze
# ---------------------------------------------------------------------------

class TestJobServiceAnalyze:
    def test_unknown_job_returns_error(self, app):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                result = JobService.analyze("nonexistent", d, {})
        assert "error" in result

    def test_missing_audio_returns_error(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "no audio")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.get_original_audio_path.return_value = None
                    result = JobService.analyze(job_id, d, {})

        assert result == {"error": "Aucun fichier audio"}

    def test_sets_state_to_analyzed(self, app, owner_id):
        from transcria.jobs.store import JobStore

        with tempfile.TemporaryDirectory() as d:
            fake_audio = Path(d) / "test.wav"
            fake_audio.write_bytes(b"RIFF")

            with app.app_context():
                info = JobService.create(owner_id, "analyze ok")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.get_original_audio_path.return_value = fake_audio
                    MockFs.return_value.save_json.return_value = None
                    with patch("transcria.audio.analyzer.AudioAnalyzer.analyze", return_value={"duration_seconds": 120}):
                        with patch("transcria.audio.preflight.AudioPreflightAnalyzer") as MockPf:
                            MockPf.return_value.enabled = False
                            with patch("transcria.quality.audio_quality.AudioQualityEvaluator") as MockQe:
                                MockQe.return_value.evaluate.return_value = {}
                                result = JobService.analyze(job_id, d, {})

                job = JobStore.get_by_id(job_id)
                assert job.state == JobState.ANALYZED.value
                assert result["duration_seconds"] == 120

    def test_preflight_exception_does_not_crash(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            fake_audio = Path(d) / "broken.wav"
            fake_audio.write_bytes(b"")

            with app.app_context():
                info = JobService.create(owner_id, "preflight crash")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.get_original_audio_path.return_value = fake_audio
                    MockFs.return_value.save_json.return_value = None
                    with patch("transcria.audio.analyzer.AudioAnalyzer.analyze", return_value={}):
                        with patch("transcria.audio.preflight.AudioPreflightAnalyzer") as MockPf:
                            MockPf.return_value.enabled = True
                            MockPf.return_value.analyze.side_effect = RuntimeError("preflight boom")
                            with patch("transcria.quality.audio_quality.AudioQualityEvaluator") as MockQe:
                                MockQe.return_value.evaluate.return_value = {}
                                result = JobService.analyze(job_id, d, {})

        assert "error" not in result

    def test_quality_evaluator_exception_does_not_crash(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            fake_audio = Path(d) / "q.wav"
            fake_audio.write_bytes(b"")

            with app.app_context():
                info = JobService.create(owner_id, "quality crash")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.get_original_audio_path.return_value = fake_audio
                    MockFs.return_value.save_json.return_value = None
                    with patch("transcria.audio.analyzer.AudioAnalyzer.analyze", return_value={}):
                        with patch("transcria.audio.preflight.AudioPreflightAnalyzer") as MockPf:
                            MockPf.return_value.enabled = False
                            with patch("transcria.quality.audio_quality.AudioQualityEvaluator") as MockQe:
                                MockQe.return_value.evaluate.side_effect = ValueError("quality boom")
                                result = JobService.analyze(job_id, d, {})

        assert "error" not in result


# ---------------------------------------------------------------------------
# JobService.get_context
# ---------------------------------------------------------------------------

class TestJobServiceGetContext:
    def test_returns_all_expected_keys(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "ctx test")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.load_text.return_value = None
                    MockFs.return_value.load_json.return_value = None
                    result = JobService.get_context(job_id, d)

        expected_keys = {
            "job", "summary", "meeting_context", "lexicon",
            "speakers", "speaker_count", "speaker_mapping",
            "participants", "analysis", "quality_report",
        }
        assert expected_keys <= set(result.keys())

    def test_summary_text_is_preserved(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "summary test")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.load_text.return_value = "mon résumé"
                    MockFs.return_value.load_json.return_value = None
                    result = JobService.get_context(job_id, d)

        assert result["summary"] == "mon résumé"

    def test_none_loads_produce_empty_defaults(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "defaults test")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.load_text.return_value = None
                    MockFs.return_value.load_json.return_value = None
                    result = JobService.get_context(job_id, d)

        assert result["summary"] == ""
        assert result["meeting_context"] == {}
        assert result["lexicon"] == []
        assert result["speakers"] == []
        assert result["speaker_count"] == 0
        assert result["participants"] == []

    def test_job_object_is_returned(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "job obj test")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.load_text.return_value = None
                    MockFs.return_value.load_json.return_value = None
                    result = JobService.get_context(job_id, d)

        assert result["job"] is not None
        assert result["job"].id == job_id


# ---------------------------------------------------------------------------
# JobService.delete
# ---------------------------------------------------------------------------

class TestJobServiceDelete:
    def test_returns_false_for_unknown_job(self, app):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                result = JobService.delete("nonexistent-xyz", d)
        assert result is False

    def test_returns_true_and_removes_job(self, app, owner_id):
        from transcria.jobs.store import JobStore

        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "to delete")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.cleanup.return_value = None
                    result = JobService.delete(job_id, d)

                assert result is True
                assert JobStore.get_by_id(job_id) is None

    def test_delete_calls_filesystem_cleanup(self, app, owner_id):
        with tempfile.TemporaryDirectory() as d:
            with app.app_context():
                info = JobService.create(owner_id, "cleanup call")
                job_id = info["job_id"]

                with patch("transcria.services.job_service.JobFilesystem") as MockFs:
                    MockFs.return_value.cleanup.return_value = None
                    JobService.delete(job_id, d)

                MockFs.return_value.cleanup.assert_called_once()

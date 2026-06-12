import tempfile
import zipfile
from pathlib import Path

import pytest

from transcria.exports.package_builder import PackageBuilder
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState


class TestPackageBuilder:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def _prepare_job(self, jobs_dir: str, job_id: str) -> Job:
        fs = JobFilesystem(jobs_dir, job_id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nHello\n")
        fs.save_json("context/meeting_context.json", {"title": "Meeting"})
        fs.save_json("quality/quality_report.json", {"score": 90})
        fs.save_text("context/job_context.yaml", "job_id: test\n")
        return Job(id=job_id, owner_id="u1", title="Test", state=JobState.CREATED.value)

    def test_build_package_creates_zip(self, tmp_dir):
        job = self._prepare_job(tmp_dir, "job-pkg-1")
        config = {"storage": {"jobs_dir": tmp_dir}}
        builder = PackageBuilder(config)
        result = builder.build_package(job)

        assert result["zip_name"] == "transcrIA_job_job-pkg-1.zip"
        assert result["size_mb"] >= 0
        zip_path = Path(result["zip_path"])
        assert zip_path.is_file()

    def test_zip_contains_expected_files(self, tmp_dir):
        job = self._prepare_job(tmp_dir, "job-pkg-2")
        config = {"storage": {"jobs_dir": tmp_dir}}
        builder = PackageBuilder(config)
        result = builder.build_package(job)

        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
            assert any("subtitles/transcription.srt" in n for n in names)
            assert any("context/job_context.yaml" in n for n in names)
            assert any("quality/quality_report.json" in n for n in names)

    def test_build_package_no_srt_still_works(self, tmp_dir):
        fs = JobFilesystem(tmp_dir, "job-minimal")
        fs.save_json("context/meeting_context.json", {"title": "Min"})
        job = Job(id="job-minimal", owner_id="u1", title="Minimal", state=JobState.CREATED.value)
        config = {"storage": {"jobs_dir": tmp_dir}}
        builder = PackageBuilder(config)
        result = builder.build_package(job)
        assert Path(result["zip_path"]).is_file()

    def test_zip_contains_summary_and_final_review_report(self, tmp_dir):
        """Le résumé et le rapport de relecture finale sont des LIVRABLES : ils
        manquaient au paquet final (passe qualité 12/06/2026)."""
        job = self._prepare_job(tmp_dir, "job-pkg-3")
        fs = JobFilesystem(tmp_dir, "job-pkg-3")
        fs.save_text("summary/summary.md", "# Résumé de contrôle\nLa réunion a décidé X.")
        fs.save_text("metadata/final_review_report.md", "## Relecture finale\nRAS.")
        config = {"storage": {"jobs_dir": tmp_dir}}
        result = PackageBuilder(config).build_package(job)

        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
            assert "summary/summary.md" in names
            assert "quality/final_review_report.md" in names

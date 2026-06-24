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

    def _prepare_rich_job(self, jobs_dir: str, job_id: str, profile_id: str) -> Job:
        fs = JobFilesystem(jobs_dir, job_id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nHello\n")
        fs.save_json("metadata/transcription_segments.json", [{"start": 1, "end": 4, "text": "Hello"}])
        fs.save_text("context/job_context.yaml", "job_id: test\n")
        fs.save_json("quality/quality_report.json", {"quality_score": 90})
        fs.save_text("summary/summary.md", "# Résumé")
        job = Job(id=job_id, owner_id="u1", title="Test", state=JobState.CREATED.value)
        job.set_extra_data({"execution": {"processing_profile_id": profile_id}})
        return job

    def test_zip_minimal_srt_express_exclut_contexte_qualite_et_docx(self, tmp_dir):
        # Phase 7 : un profil léger ne sur-livre plus (pas de contexte/qualité/DOCX).
        job = self._prepare_rich_job(tmp_dir, "job-srt-express", "srt_express")
        result = PackageBuilder({"storage": {"jobs_dir": tmp_dir}}).build_package(job)
        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
        assert any("subtitles/transcription.srt" in n for n in names)
        assert any("transcription_segments.json" in n for n in names)
        assert not any("context/job_context.yaml" in n for n in names)
        assert not any("quality/quality_report.json" in n for n in names)
        assert not any(n.endswith(".docx") for n in names)

    def test_zip_standard_word_corrige_inclut_contexte_sans_rapport_qualite(self, tmp_dir):
        job = self._prepare_rich_job(tmp_dir, "job-word-corrige", "word_corrige")
        result = PackageBuilder({"storage": {"jobs_dir": tmp_dir}}).build_package(job)
        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
        assert any("context/job_context.yaml" in n for n in names)
        assert any("summary/summary.md" in n for n in names)
        # zip_level=standard → pas le groupe qualité complet.
        assert not any("quality/quality_report.json" in n for n in names)

    def test_zip_full_dossier_qualite_inclut_tout(self, tmp_dir):
        job = self._prepare_rich_job(tmp_dir, "job-dossier", "dossier_qualite")
        result = PackageBuilder({"storage": {"jobs_dir": tmp_dir}}).build_package(job)
        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
        assert any("context/job_context.yaml" in n for n in names)
        assert any("quality/quality_report.json" in n for n in names)

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

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

    def test_zip_garde_le_srt_brut_a_cote_du_corrige(self, tmp_dir):
        """Réunion contestée : le ZIP doit permettre de comparer ce que l'ASR a entendu
        (brut) et ce que la LLM a corrigé — le brut n'est PAS remplacé par la corrigée."""
        job = self._prepare_job(tmp_dir, "job-pkg-raw")
        fs = JobFilesystem(tmp_dir, "job-pkg-raw")
        fs.save_text("metadata/transcription_corrigee.srt",
                     "1\n00:00:01,000 --> 00:00:04,000\nHello, corrected\n")
        config = {"storage": {"jobs_dir": tmp_dir}}
        result = PackageBuilder(config).build_package(job)

        with zipfile.ZipFile(result["zip_path"], "r") as zf:
            names = zf.namelist()
            assert "subtitles/transcription.srt" in names
            assert "subtitles/transcription_raw.srt" in names
            assert "corrected" in zf.read("subtitles/transcription.srt").decode("utf-8")
            assert "Hello\n" in zf.read("subtitles/transcription_raw.srt").decode("utf-8")

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


class TestVerifyPackage:
    """Garde-fou d'ouvrabilité des livrables (PackageBuilder.verify_package)."""

    def test_valid_zip_no_issues(self, tmp_path):
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.txt", "hello")
        assert PackageBuilder.verify_package(z) == []

    def test_corrupt_zip_detected(self, tmp_path):
        z = tmp_path / "bad.zip"
        z.write_bytes(b"ceci n'est pas un zip")
        issues = PackageBuilder.verify_package(z)
        assert any("ZIP" in i for i in issues)

    def test_valid_docx_no_issues(self, tmp_path):
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("x", "1")
        d = tmp_path / "rapport.docx"
        with zipfile.ZipFile(d, "w") as zf:
            zf.writestr("[Content_Types].xml", "<xml/>")
        assert PackageBuilder.verify_package(z, d) == []

    def test_unreadable_docx_detected(self, tmp_path):
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("x", "1")
        d = tmp_path / "rapport.docx"
        d.write_bytes(b"PK\x03\x04 placeholder non ouvrable")
        issues = PackageBuilder.verify_package(z, d)
        assert any("DOCX" in i for i in issues)

    def test_docx_zip_without_content_types_detected(self, tmp_path):
        z = tmp_path / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("x", "1")
        d = tmp_path / "rapport.docx"
        with zipfile.ZipFile(d, "w") as zf:  # zip valide mais pas un conteneur OOXML
            zf.writestr("word/document.xml", "<xml/>")
        issues = PackageBuilder.verify_package(z, d)
        assert any("Content_Types" in i for i in issues)


class TestEffectiveSummaryMarkdown:
    """Le summary.md du PACKAGE reflète l'édition manuelle de l'étape 4 (bug réel :
    l'édition n'atteignait que le DOCX, le ZIP embarquait le brut LLM)."""

    RAW = "# Résumé\n\n## Synthèse\n\nTexte GÉNÉRÉ par la LLM.\n\n## Points clés\n- a\n- b\n"

    def test_sans_edition_le_brut_est_conserve(self):
        from transcria.context.meeting_context import MeetingContextManager
        assert MeetingContextManager.effective_summary_markdown({}, self.RAW) == self.RAW

    def test_edition_manuelle_remplace_la_section_synthese(self):
        from transcria.context.meeting_context import MeetingContextManager
        out = MeetingContextManager.effective_summary_markdown({"summary": "Texte ÉDITÉ main."}, self.RAW)
        assert "Texte ÉDITÉ main." in out
        assert "Texte GÉNÉRÉ par la LLM." not in out
        assert "## Points clés" in out          # le reste de la structure survit
        assert out.startswith("# Résumé")

    def test_harmonise_utilise_apres_manuel(self):
        from transcria.context.meeting_context import MeetingContextManager
        ctx = {"summary": "MANUEL.", "summary_harmonized": "HARMONISÉ."}
        assert "MANUEL." in MeetingContextManager.effective_summary_markdown(ctx, self.RAW)
        ctx = {"summary_harmonized": "HARMONISÉ."}
        assert "HARMONISÉ." in MeetingContextManager.effective_summary_markdown(ctx, self.RAW)

    def test_brut_sans_section_synthese_remplace_tout(self):
        from transcria.context.meeting_context import MeetingContextManager
        out = MeetingContextManager.effective_summary_markdown({"summary": "ÉDITÉ."}, "Texte brut sans section.")
        assert out == "ÉDITÉ.\n"

    def test_package_zip_embarque_le_resume_effectif(self, tmp_path):
        from transcria.context.meeting_context import MeetingContextManager
        job_id = "job-summary-eff"
        jobs_dir = str(tmp_path)
        fs = JobFilesystem(jobs_dir, job_id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
        fs.save_text("summary/summary.md", self.RAW)
        job = Job(id=job_id, owner_id="u1", title="Résumé", state=JobState.CREATED.value)
        MeetingContextManager.save(job, jobs_dir, {"summary": "Synthèse CORRIGÉE par la secrétaire."})
        result = PackageBuilder({"storage": {"jobs_dir": jobs_dir}}).build_package(job)
        with zipfile.ZipFile(result["zip_path"]) as z:
            md = z.read("summary/summary.md").decode("utf-8")
        assert "Synthèse CORRIGÉE par la secrétaire." in md
        assert "Texte GÉNÉRÉ par la LLM." not in md

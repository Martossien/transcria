import logging
import re
import zipfile

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class PackageBuilder:
    def __init__(self, config: dict):
        self.config = config

    def build_package(self, job: Job) -> dict:
        jobs_dir = self.config.get("storage", {}).get("jobs_dir", "./jobs")
        fs = JobFilesystem(jobs_dir, job.id)
        export_dir = fs.job_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_name = f"transcrIA_job_{job.id}.zip"
        zip_path = export_dir / zip_name

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                self._add_file(zf, fs, "input", "audio/")
                self._add_if_exists(zf, fs, "metadata/transcription_corrigee.srt", "subtitles/transcription.srt")
                if not (fs.job_dir / "metadata" / "transcription_corrigee.srt").is_file():
                    self._add_if_exists(zf, fs, "metadata/transcription.srt", "subtitles/transcription.srt")
                self._add_if_exists(zf, fs, "metadata/transcription_segments.json", "subtitles/transcription_segments.json")
                self._add_if_exists(zf, fs, "context/job_context.yaml", "context/job_context.yaml")
                self._add_if_exists(zf, fs, "context/meeting_context.json", "context/meeting_context.json")
                self._add_if_exists(zf, fs, "context/participants.json", "context/participants.json")
                self._add_if_exists(zf, fs, "context/session_lexicon.json", "context/session_lexicon.json")
                self._add_if_exists(zf, fs, "speakers/speaker_mapping.json", "context/speaker_mapping.json")
                self._add_if_exists(zf, fs, "speakers/speaker_stats.json", "context/speaker_stats.json")
                self._add_if_exists(zf, fs, "quality/quality_report.md", "quality/quality_report.md")
                self._add_if_exists(zf, fs, "quality/quality_report.json", "quality/quality_report.json")
                self._add_if_exists(zf, fs, "quality/review_points.json", "quality/review_points.json")
                self._add_if_exists(zf, fs, "metadata/correction_report.md", "quality/correction_report.md")
                self._add_docx_report(zf, fs, job)
        except Exception as exc:
            logger.exception("Échec création package ZIP")
            return {"error": str(exc), "zip_path": str(zip_path), "zip_name": zip_name, "size_mb": 0}

        size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
        return {
            "zip_path": str(zip_path),
            "zip_name": zip_name,
            "size_mb": size_mb,
        }

    def _add_file(self, zf: zipfile.ZipFile, fs: JobFilesystem, rel_dir: str, zip_prefix: str) -> None:
        src_dir = fs.job_dir / rel_dir
        if not src_dir.is_dir():
            return
        for file in sorted(src_dir.iterdir()):
            if file.is_file():
                zf.write(file, zip_prefix + file.name)

    def _add_if_exists(self, zf: zipfile.ZipFile, fs: JobFilesystem, rel_path: str, zip_path: str) -> None:
        src = fs.job_dir / rel_path
        if src.is_file():
            zf.write(src, zip_path)

    def _add_docx_report(self, zf: zipfile.ZipFile, fs: JobFilesystem, job: Job) -> None:
        jobs_dir = self.config.get("storage", {}).get("jobs_dir", "./jobs")
        safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
        docx_path = fs.job_dir / "exports" / f"rapport_{safe_title}.docx"
        try:
            from transcria.exports.docx_report import generate_docx_report
            generate_docx_report(job.id, jobs_dir, docx_path)
            zf.write(docx_path, f"rapport_{safe_title}.docx")
        except Exception:
            logger.warning("Impossible de générer le rapport DOCX pour le job %s — ignoré dans le ZIP", job.id)

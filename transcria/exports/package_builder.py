import logging
import re
import zipfile
from pathlib import Path

from transcria.context.meeting_context import MeetingContextManager
from transcria.exports.docx_report import generate_docx_report
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.workflow.profiles import profile_for_job

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

        # Niveaux selon le profil (Phase 7). Job legacy / sans profil → comportement complet
        # (full), strictement identique à l'historique : aucune régression de livrable.

        profile = profile_for_job(job)
        zip_level = profile.zip_level if profile is not None else "full"
        docx_level = profile.docx_level if profile is not None else "full"

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # — Minimal (tous niveaux) : audio + sous-titres + segments —
                self._add_file(zf, fs, "input", "audio/")
                self._add_if_exists(zf, fs, "metadata/transcription_corrigee.srt", "subtitles/transcription.srt")
                if not (fs.job_dir / "metadata" / "transcription_corrigee.srt").is_file():
                    self._add_if_exists(zf, fs, "metadata/transcription.srt", "subtitles/transcription.srt")
                else:
                    # Transcription BRUTE (pré-correction) conservée à côté de la corrigée :
                    # pour les réunions contestées, on veut pouvoir comparer ce que l'ASR a
                    # entendu et ce que la LLM a corrigé (en plus de correction_report.md).
                    self._add_if_exists(zf, fs, "metadata/transcription.srt", "subtitles/transcription_raw.srt")
                self._add_if_exists(zf, fs, "metadata/transcription_segments.json", "subtitles/transcription_segments.json")
                # — Standard et + : contexte, participants, locuteurs, résumé —
                if zip_level in ("standard", "full"):
                    self._add_if_exists(zf, fs, "context/job_context.yaml", "context/job_context.yaml")
                    self._add_if_exists(zf, fs, "context/meeting_context.json", "context/meeting_context.json")
                    self._add_if_exists(zf, fs, "context/participants.json", "context/participants.json")
                    self._add_if_exists(zf, fs, "context/session_lexicon.json", "context/session_lexicon.json")
                    self._add_if_exists(zf, fs, "speakers/speaker_mapping.json", "context/speaker_mapping.json")
                    self._add_if_exists(zf, fs, "speakers/speaker_stats.json", "context/speaker_stats.json")
                    # summary.md du LIVRABLE = résumé EFFECTIF : l'édition manuelle de
                    # l'étape 4 (ou l'harmonisation) remplace la section Synthèse — le
                    # fichier interne summary/summary.md reste le brut LLM (référence
                    # des phases aval). Bug réel : l'édition n'atteignait que le DOCX.
                    raw_summary = fs.load_text("summary/summary.md")
                    if raw_summary is not None:
                        meeting_ctx_pkg = fs.load_json("context/meeting_context.json") or {}
                        zf.writestr("summary/summary.md",
                                    MeetingContextManager.effective_summary_markdown(meeting_ctx_pkg, raw_summary))
                # — Full uniquement : rapports qualité / correction / relecture —
                if zip_level == "full":
                    self._add_if_exists(zf, fs, "quality/quality_report.md", "quality/quality_report.md")
                    self._add_if_exists(zf, fs, "quality/quality_report.json", "quality/quality_report.json")
                    self._add_if_exists(zf, fs, "quality/review_points.json", "quality/review_points.json")
                    self._add_if_exists(zf, fs, "metadata/correction_report.md", "quality/correction_report.md")
                    self._add_if_exists(zf, fs, "metadata/final_review_report.md", "quality/final_review_report.md")
                if docx_level != "none":
                    self._add_docx_report(zf, fs, job)
        except Exception as exc:
            logger.exception("Échec création package ZIP")
            return {"error": str(exc), "zip_path": str(zip_path), "zip_name": zip_name, "size_mb": 0}

        docx_path: Path | None = None
        if docx_level != "none":
            safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
            docx_path = export_dir / f"rapport_{safe_title}.docx"
        integrity_issues = self.verify_package(zip_path, docx_path)
        if integrity_issues:
            logger.warning("Intégrité des livrables job %s : %s", job.id, "; ".join(integrity_issues))

        size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
        return {
            "zip_path": str(zip_path),
            "zip_name": zip_name,
            "size_mb": size_mb,
            "integrity_issues": integrity_issues,
        }

    @staticmethod
    def verify_package(zip_path: Path, docx_path: Path | None = None) -> list[str]:
        """Garde-fou export : les livrables produits sont-ils réellement OUVRABLES ?

        ZIP : conteneur lisible + intégrité CRC (`testzip`). DOCX : conteneur OOXML valide
        (lisible comme zip + `[Content_Types].xml` présent). Retourne la liste des anomalies
        (vide = livrables sains). Détection, pas d'échec dur — un export corrompu est loggé
        et remonté dans `integrity_issues`.
        """
        issues: list[str] = []
        if not zipfile.is_zipfile(zip_path):
            issues.append("ZIP illisible ou corrompu")
        else:
            with zipfile.ZipFile(zip_path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    issues.append(f"ZIP corrompu (CRC invalide) : {bad}")
        if docx_path is not None and docx_path.is_file():
            if not zipfile.is_zipfile(docx_path):
                issues.append("DOCX illisible (conteneur OOXML invalide)")
            else:
                with zipfile.ZipFile(docx_path) as zf:
                    if "[Content_Types].xml" not in zf.namelist():
                        issues.append("DOCX invalide ([Content_Types].xml absent)")
        return issues

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
            generate_docx_report(job.id, jobs_dir, docx_path)
            zf.write(docx_path, f"rapport_{safe_title}.docx")
        except Exception:
            logger.warning("Impossible de générer le rapport DOCX pour le job %s — ignoré dans le ZIP", job.id)

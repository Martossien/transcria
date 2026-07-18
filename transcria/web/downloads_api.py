"""API JSON de téléchargement : SRT, package ZIP, audio, DOCX, extraits audio et
clips locuteurs.

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``.
"""
import logging
import re
from pathlib import Path

from flask import abort, jsonify, request, send_file
from flask_login import login_required

from transcria.audio.excerpts import AudioExcerptService
from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.config import get_config
from transcria.exports.docx_report import generate_docx_report
from transcria.exports.package_builder import PackageBuilder
from transcria.jobs import artifact_store
from transcria.jobs.filesystem import JobFilesystem
from transcria.web.blueprint import web_bp
from transcria.web.job_access import get_job_for_api
from transcria.web.lexicon_views import resolve_context_audio_range
from transcria.web.request_helpers import api_stable

logger = logging.getLogger(__name__)


def _local_newest_artifact_mtime_ns(cfg: dict, job_id: str) -> int:
    """mtime (ns) le plus récent des artefacts SOURCES du package, backend local.

    Périmètre = les répertoires que PackageBuilder lit (metadata, context, speakers,
    summary, quality) — exports/ exclu (c'est la sortie). 0 si rien de lisible."""
    job_dir = Path(cfg["storage"]["jobs_dir"]) / job_id
    newest = 0
    for sub in ("metadata", "context", "speakers", "summary", "quality"):
        base = job_dir / sub
        if not base.is_dir():
            continue
        for item in base.rglob("*"):
            try:
                if item.is_file():
                    newest = max(newest, item.stat().st_mtime_ns)
            except OSError:
                continue
    return newest


def _resolve_speaker_clip(samples_dir: Path, raw_clip: str) -> tuple[str, Path] | None:
    """Résout une référence d'extrait locuteur sans exposer les chemins disque."""
    raw_clip = (raw_clip or "").strip()
    if not raw_clip:
        return None

    raw_path = Path(raw_clip)
    clip_path = raw_path.resolve() if raw_path.is_absolute() else (samples_dir / raw_path).resolve()
    if not clip_path.is_relative_to(samples_dir) or not clip_path.is_file():
        return None

    return clip_path.relative_to(samples_dir).as_posix(), clip_path


@web_bp.route("/api/jobs/<job_id>/download/srt", methods=["GET"])
@login_required
@api_stable
def api_download_srt(job_id: str):
    """Télécharge le sous-titrage corrigé (SRT — contrat scriptable)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    srt_path = fs.job_dir / "metadata" / "transcription_corrigee.srt"
    if not srt_path.is_file():
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
    if not srt_path.is_file():
        abort(404)

    safe_title = job.title.replace(" ", "_")[:50]
    audit_log(AuditAction.JOB_DOWNLOAD, target_type="job", target_id=job.id, target_label=job.title, details={"format": "srt"})
    return send_file(
        srt_path,
        as_attachment=True,
        download_name=f"{safe_title}_transcription.srt",
        mimetype="text/plain",
    )


@web_bp.route("/api/jobs/<job_id>/download/package", methods=["GET"])
@login_required
@api_stable
def api_download_package(job_id: str):
    """Télécharge le paquet complet des livrables (ZIP — contrat scriptable)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    zip_path = Path(cfg["storage"]["jobs_dir"]) / job.id / "exports" / f"transcrIA_job_{job.id}.zip"
    if artifact_store.is_pg_backend(cfg):
        # Backend `pg` : le zip n'est PAS transporté en base (il contient l'audio). On le
        # (re)construit localement depuis les artefacts matérialisés, s'il est absent ou
        # plus vieux que le dernier artefact synchronisé (ex. job retraité par le worker).
        stale = (not zip_path.is_file()) or (
            zip_path.stat().st_mtime_ns < artifact_store.newest_synced_mtime_ns(cfg, job.id)
        )
    else:
        # Backend fichiers local (§5.3) : même honnêteté qu'en `pg` — le zip servi ne
        # doit jamais être plus vieux que les artefacts sources (édition SRT dont la
        # reconstruction best-effort a échoué, artefact modifié hors reconstruction).
        # Un job SANS artefact source (newest == 0) reste un 404 franc : rien à
        # reconstruire, ne pas transformer l'absence en 500 de PackageBuilder.
        newest = _local_newest_artifact_mtime_ns(cfg, job.id)
        stale = (newest > 0) and (
            (not zip_path.is_file()) or zip_path.stat().st_mtime_ns < newest
        )
    if stale:
        build = PackageBuilder(cfg).build_package(job)
        if build.get("error"):
            logger.error("Reconstruction locale du package impossible: job=%s erreur=%s",
                         job.id, build["error"])
            abort(500)
    if not zip_path.is_file():
        abort(404)

    audit_log(AuditAction.JOB_DOWNLOAD, target_type="job", target_id=job.id, target_label=job.title, details={"format": "zip"})
    return send_file(zip_path, as_attachment=True, download_name=zip_path.name, mimetype="application/zip")


@web_bp.route("/api/jobs/<job_id>/download/audio", methods=["GET"])
@login_required
def api_download_audio(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        abort(404)

    audit_log(AuditAction.JOB_DOWNLOAD, target_type="job", target_id=job.id, target_label=job.title, details={"format": "audio"})
    return send_file(audio_path, as_attachment=True, download_name=audio_path.name)


@web_bp.route("/api/jobs/<job_id>/download/docx", methods=["GET"])
@login_required
@api_stable
def api_download_docx(job_id: str):
    """Télécharge le compte rendu Word (DOCX — contrat scriptable).

    Profils SRT (docx_level "none") : DOCX VERBATIM généré à la demande — le
    générateur dégrade proprement les sections sans artefact (pas de synthèse
    sans LLM, pas de tableau locuteurs sans diarisation). Le ZIP du profil
    reste minimal : seule cette route produit le document, rien n'est promis
    par le pipeline (PISTES_AMELIORATION §5.1).
    """
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    # Historique : les profils SRT recevaient un 404 ici. Depuis 0.3.8, la
    # génération à la demande est PERMISE (capacité additive : l'utilisateur qui a
    # édité son SRT dans l'éditeur peut emporter un DOCX verbatim) — les LIVRABLES
    # du profil (ZIP minimal, aucune passe LLM) ne changent pas.

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
    docx_path = fs.job_dir / "exports" / f"rapport_{safe_title}.docx"

    try:
        generate_docx_report(job.id, cfg["storage"]["jobs_dir"], docx_path)
    except Exception:
        logger.exception("Échec génération rapport DOCX pour le job %s", job.id)
        abort(500)

    audit_log(
        AuditAction.JOB_DOWNLOAD,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={"format": "docx"},
    )
    return send_file(
        docx_path,
        as_attachment=True,
        download_name=f"{safe_title}_rapport.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@web_bp.route("/api/jobs/<job_id>/audio/excerpt", methods=["GET"])
@login_required
def api_audio_excerpt(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Fichier audio introuvable"}), 404

    timecode = request.args.get("timecode", "")
    quote = request.args.get("quote", "")
    summary_data = fs.load_json("summary/summary.json") or {}
    segments = summary_data.get("segments") if isinstance(summary_data, dict) else []
    resolved = resolve_context_audio_range(timecode, quote, segments)  # type: ignore[arg-type]
    if resolved is None:
        return jsonify({"error": "Timecode audio invalide"}), 400
    start_s, end_s, corrected = resolved
    if corrected:
        logger.info(
            "Timecode contexte lexique ajusté depuis la citation: job=%s original=%r start=%.3f end=%.3f",
            job.id,
            timecode,
            start_s,
            end_s,
        )

    try:
        pad = float(request.args.get("pad", "5"))
        max_duration = float(request.args.get("max_duration", "90"))
        excerpt_path = AudioExcerptService.build_excerpt(
            audio_path,
            fs.job_dir / "metadata" / "audio_excerpts",
            start_s,
            end_s,
            pad_s=min(max(pad, 0.0), 15.0),
            max_duration_s=min(max(max_duration, 5.0), 120.0),
        )
    except ValueError as exc:
        logger.warning("Demande extrait audio invalide: job=%s timecode=%r erreur=%s", job.id, timecode, exc)
        return jsonify({"error": "Paramètres audio invalides"}), 400
    except (FileNotFoundError, RuntimeError) as exc:
        logger.warning("Extrait audio indisponible: job=%s timecode=%r erreur=%s", job.id, timecode, exc)
        return jsonify({"error": "Extrait audio indisponible"}), 500

    audit_log(
        AuditAction.JOB_DOWNLOAD,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "format": "audio_excerpt",
            "start_s": round(start_s, 3),
            "end_s": round(end_s, 3),
            "duration_s": round(max(0.0, end_s - start_s), 3),
            "timecode_corrected": bool(corrected),
        },
    )
    return send_file(excerpt_path, mimetype="audio/wav", conditional=True)


@web_bp.route("/api/jobs/<job_id>/speakers/clips", methods=["GET"])
@login_required
def api_speaker_clips(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    clips = fs.load_json("speakers/speaker_clips.json") or {}
    samples_dir = (fs.job_dir / "speakers" / "samples").resolve()
    safe_clips: dict[str, list[str]] = {}
    if isinstance(clips, dict):
        for speaker_id, raw_paths in clips.items():
            if not isinstance(raw_paths, list):
                continue
            clip_names = []
            for raw_path in raw_paths:
                resolved = _resolve_speaker_clip(samples_dir, str(raw_path))
                if resolved is not None:
                    clip_names.append(resolved[0])
            safe_clips[str(speaker_id)] = clip_names
    return jsonify({"clips": safe_clips})


@web_bp.route("/api/jobs/<job_id>/speakers/clip/<path:clip_name>", methods=["GET"])
@login_required
def api_speaker_clip_file(job_id: str, clip_name: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    samples_dir = (fs.job_dir / "speakers" / "samples").resolve()
    resolved = _resolve_speaker_clip(samples_dir, clip_name)
    if resolved is None:
        abort(404)
    public_name, clip_path = resolved
    audit_log(
        AuditAction.JOB_DOWNLOAD,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "format": "speaker_clip",
            "clip_name": public_name,
        },
    )
    return send_file(clip_path, mimetype="audio/wav")

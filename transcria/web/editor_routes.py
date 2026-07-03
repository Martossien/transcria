"""Éditeur de transcription intégré — API serveur (lot A, docs/EDITEUR_SRT_INTEGRE.md §3.4).

Contrats clés :
- ``state``  : tout ce que l'atelier doit savoir en UN appel (chunks, locuteurs,
  brouillon, points qualité, audio, lecture seule) ;
- ``draft``  : filet anti-crash (D2) — verrou OPTIMISTE par ``revision`` (409 si un
  autre onglet a écrit depuis), JAMAIS d'autre erreur bloquante ;
- ``save``   : « Enregistrer une version » — garde de FORME (l'humain a le droit de
  supprimer/fusionner), snapshot dans le POOL COMMUN de versions (RefineStore, partagé
  avec le chat d'affinage), write-back SRT + recalcul des stats locuteurs (A2),
  purge du brouillon, audit en métadonnées seulement ;
- ``stream`` : audio original inline avec Range (seek) — 404 propre si non
  matérialisable (mode dégradé A1) ;
- ``peaks``  : pics de waveform (cache job, génération paresseuse en thread, 202
  pendant le calcul).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file
from flask_login import login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.config import get_config
from transcria.jobs.filesystem import JobFilesystem
from transcria.workflow.refine_store import RefineStore
from transcria.workflow.srt_editor import (
    SrtParseError,
    compute_speaker_stats,
    parse_srt_chunks,
    serialize_chunks,
    validate_chunks,
)
from transcria.workflow.transitions import is_execution_active
from transcria.workflow.waveform_peaks import generate_peaks, peaks_paths, peaks_ready

editor_bp = Blueprint("srt_editor", __name__)
logger = logging.getLogger(__name__)

_DRAFT_REL = "metadata/srt_editor_draft.json"
_MAX_CHUNKS = 20000          # garde absurde (4 h 30 réel ≈ 3 000)
_MAX_TEXT_LEN = 4000         # par chunk — bien au-delà du réel, borne anti-abus

# Générations de pics en cours (clé = job_id) — évite les doubles ffmpeg.
_PEAKS_RUNNING: set[str] = set()
_PEAKS_LOCK = threading.Lock()


def _fs(job_id: str) -> JobFilesystem:
    return JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)


def _get_job(job_id: str):
    """Accès identique au reste des pages job (propriétaire/groupes/admin)."""
    from transcria.web.routes import _get_job_for_api

    return _get_job_for_api(job_id)


def _effective_srt_text(fs: JobFilesystem) -> str | None:
    return fs.load_text("metadata/transcription_corrigee.srt") or fs.load_text("metadata/transcription.srt")


def _is_readonly(job, fs: JobFilesystem) -> bool:
    """Lecture seule si le pipeline OU un tour d'affinage écrit potentiellement le SRT."""
    if is_execution_active(job):
        return True
    store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=job.id)
    return store.has_active_request()


def _audio_path(fs: JobFilesystem) -> Path | None:
    return fs.get_original_audio_path()


def _audio_duration_ms(fs: JobFilesystem) -> int | None:
    analysis = fs.load_json("metadata/audio_analysis.json") or {}
    try:
        return int(float(analysis["duration_seconds"]) * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def _clean_chunks(raw: object) -> list[dict]:
    """Valide la FORME du payload client (types/bornes) — lève ValueError explicite."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("chunks : liste non vide attendue")
    if len(raw) > _MAX_CHUNKS:
        raise ValueError(f"chunks : {len(raw)} segments (maximum {_MAX_CHUNKS})")
    chunks: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"segment {i + 1} : objet attendu")
        try:
            start_ms, end_ms = int(entry["start_ms"]), int(entry["end_ms"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"segment {i + 1} : start_ms/end_ms entiers requis") from None
        if start_ms < 0 or end_ms < 0:
            raise ValueError(f"segment {i + 1} : timestamps négatifs")
        text = str(entry.get("text") or "")
        if len(text) > _MAX_TEXT_LEN:
            raise ValueError(f"segment {i + 1} : texte trop long ({len(text)} caractères)")
        speaker_id = entry.get("speaker_id")
        if speaker_id is not None and not isinstance(speaker_id, str):
            raise ValueError(f"segment {i + 1} : speaker_id invalide")
        speaker_name = entry.get("speaker_name")
        if speaker_name is not None and not isinstance(speaker_name, str):
            raise ValueError(f"segment {i + 1} : speaker_name invalide")
        chunks.append({
            "start_ms": start_ms, "end_ms": end_ms,
            "speaker_id": speaker_id or None,
            "speaker_name": (speaker_name or "").strip()[:120] or None,
            "text": text,
        })
    return chunks


# ── Page ──────────────────────────────────────────────────────────────────────

@editor_bp.route("/jobs/<job_id>/editor")
@login_required
def editor_page(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    fs = _fs(job_id)
    if _effective_srt_text(fs) is None:
        return ("Aucune transcription à éditer pour ce traitement.", 404)
    return render_template("srt_editor.html", job=job)


# ── État ──────────────────────────────────────────────────────────────────────

@editor_bp.route("/api/jobs/<job_id>/editor/state", methods=["GET"])
@login_required
def editor_state(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    fs = _fs(job_id)
    srt_text = _effective_srt_text(fs)
    if srt_text is None:
        return jsonify({"error": "Aucune transcription à éditer."}), 404
    try:
        chunks = parse_srt_chunks(srt_text)
    except SrtParseError as exc:
        return jsonify({"error": f"Transcription illisible : {exc}"}), 422

    draft_raw = fs.load_json(_DRAFT_REL)
    draft = draft_raw if isinstance(draft_raw, dict) else None
    base_sha = _sha256(srt_text)
    audio = _audio_path(fs)
    duration_ms = _audio_duration_ms(fs)
    return jsonify({
        "chunks": chunks,
        "srt_sha256": base_sha,
        "speakers": {
            "mapping": fs.load_json("speakers/speaker_mapping.json") or {},
            "stats": fs.load_json("speakers/speaker_stats.json") or {},
        },
        "draft": {
            "exists": draft is not None,
            "revision": (draft or {}).get("revision", 0),
            "updated_at": (draft or {}).get("updated_at"),
            "chunk_count": len((draft or {}).get("chunks") or []),
            # un affinage/une correction est passé depuis ce brouillon → l'UI propose
            # un choix explicite, jamais de fusion silencieuse (§3.2)
            "conflict": bool(draft and draft.get("base_srt_sha256") not in (None, base_sha)),
        },
        "review_points": fs.load_json("quality/review_points.json") or [],
        "review_anchors": fs.load_json("quality/review_points_anchors.json") or [],
        "audio": {
            "available": audio is not None,
            "duration_ms": duration_ms,
            "peaks_ready": peaks_ready(fs.job_dir),
        },
        "readonly": _is_readonly(job, fs),
        "warnings": validate_chunks(chunks, audio_duration_ms=duration_ms),
    })


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Brouillon (filet anti-crash — D2) ─────────────────────────────────────────

@editor_bp.route("/api/jobs/<job_id>/editor/draft", methods=["GET"])
@login_required
def editor_draft_get(job_id: str):
    """Contenu du brouillon (écran « Reprendre où vous en étiez »)."""
    job, error = _get_job(job_id)
    if error:
        return error
    draft = _fs(job_id).load_json(_DRAFT_REL)
    if not isinstance(draft, dict):
        return jsonify({"error": "Aucun brouillon."}), 404
    return jsonify(draft)


@editor_bp.route("/api/jobs/<job_id>/editor/draft", methods=["PUT"])
@login_required
def editor_draft_put(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    fs = _fs(job_id)
    data = request.get_json(silent=True) or {}
    try:
        chunks = _clean_chunks(data.get("chunks"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    existing_raw = fs.load_json(_DRAFT_REL)
    existing = existing_raw if isinstance(existing_raw, dict) else {}
    client_revision = int(data.get("revision") or 0)
    server_revision = int(existing.get("revision") or 0)
    if existing and client_revision != server_revision:
        # Un autre onglet/frontale a écrit depuis (P8) : celui qui a la main la garde.
        return jsonify({"error": "Brouillon modifié ailleurs.", "server_revision": server_revision}), 409

    fs.save_json(_DRAFT_REL, {
        "schema_version": 1,
        "revision": server_revision + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "base_srt_sha256": str(data.get("base_srt_sha256") or ""),
        "chunks": chunks,
        "new_speakers": data.get("new_speakers") or [],
        "markers": data.get("markers") or [],
        "progress": data.get("progress") or {},
    })
    return jsonify({"revision": server_revision + 1})


@editor_bp.route("/api/jobs/<job_id>/editor/draft", methods=["DELETE"])
@login_required
def editor_draft_delete(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    draft_path = _fs(job_id).job_dir / _DRAFT_REL
    if draft_path.exists():
        draft_path.unlink()
    return jsonify({"status": "ok"})


# ── Enregistrer une version ───────────────────────────────────────────────────

@editor_bp.route("/api/jobs/<job_id>/editor/save", methods=["POST"])
@login_required
def editor_save(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    fs = _fs(job_id)
    if _is_readonly(job, fs):
        return jsonify({"error": "Un traitement est en cours sur ce job — édition en lecture seule."}), 409
    data = request.get_json(silent=True) or {}
    try:
        chunks = _clean_chunks(data.get("chunks"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422

    new_srt = serialize_chunks(chunks)
    # Garde de FORME uniquement : le résultat doit rester un SRT que le reste du
    # produit sait relire (l'humain a le droit de tout réécrire — pas de garde de
    # volume comme pour la correction LLM).
    try:
        parse_srt_chunks(new_srt)
    except SrtParseError as exc:  # défense en profondeur — inatteignable en théorie
        return jsonify({"error": f"SRT produit invalide : {exc}"}), 422

    jobs_dir = get_config()["storage"]["jobs_dir"]
    store = RefineStore(jobs_dir=jobs_dir, job_id=job_id)
    # Snapshot AVANT write-back, dans le pool COMMUN (restauration croisée avec
    # l'affinage) : SRT + stats + mapping voyagent et se restaurent ENSEMBLE (A2).
    version = store.snapshot_artifacts([
        fs.job_dir / "metadata" / "transcription_corrigee.srt",
        fs.job_dir / "speakers" / "speaker_stats.json",
        fs.job_dir / "speakers" / "speaker_mapping.json",
    ])

    fs.save_text("metadata/transcription_corrigee.srt", new_srt)
    fs.save_json("speakers/speaker_stats.json", compute_speaker_stats(chunks))
    _merge_new_speakers(fs, data.get("new_speakers"))

    draft_path = fs.job_dir / _DRAFT_REL
    if draft_path.exists():
        draft_path.unlink()

    edited = int(data.get("edited_count") or 0)
    audit_log(AuditAction.JOB_SRT_EDIT_SAVE, target_type="job", target_id=job.id,
              target_label=job.title,
              details={"version": version, "chunks": len(chunks), "edited": edited,
                       "new_speakers": len(data.get("new_speakers") or [])})
    warnings = validate_chunks(chunks, audio_duration_ms=_audio_duration_ms(fs))
    return jsonify({"version": version, "warnings": warnings})


def _merge_new_speakers(fs: JobFilesystem, raw: object) -> None:
    """Ajoute les locuteurs créés dans l'éditeur au mapping du job (A2)."""
    if not isinstance(raw, list) or not raw:
        return
    mapping_doc = fs.load_json("speakers/speaker_mapping.json") or {}
    raw_mapping = mapping_doc.get("mapping")
    mapping: dict = raw_mapping if isinstance(raw_mapping, dict) else {}
    changed = False
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        speaker_id = str(entry.get("speaker_id") or "").strip()
        name = str(entry.get("speaker_name") or "").strip()[:120]
        if speaker_id and name and speaker_id not in mapping:
            mapping[speaker_id] = name
            changed = True
    if changed:
        mapping_doc["mapping"] = mapping
        fs.save_json("speakers/speaker_mapping.json", mapping_doc)


# ── Audio & pics ──────────────────────────────────────────────────────────────

@editor_bp.route("/api/jobs/<job_id>/audio/stream", methods=["GET"])
@login_required
def editor_audio_stream(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    audio = _audio_path(_fs(job_id))
    if audio is None:
        return jsonify({"error": "Audio non disponible sur cette installation."}), 404
    # Audité UNE fois par session d'écoute : la 1ʳᵉ requête (sans Range ou depuis 0),
    # pas chaque seek (sinon le journal se noie).
    range_header = request.headers.get("Range", "")
    if not range_header or range_header.startswith("bytes=0-"):
        audit_log(AuditAction.JOB_DOWNLOAD, target_type="job", target_id=job.id,
                  target_label=job.title, details={"kind": "editor_audio_stream"})
    return send_file(audio, conditional=True)


@editor_bp.route("/api/jobs/<job_id>/editor/peaks", methods=["GET"])
@login_required
def editor_peaks(job_id: str):
    job, error = _get_job(job_id)
    if error:
        return error
    fs = _fs(job_id)
    bin_path, _meta = peaks_paths(fs.job_dir)
    if peaks_ready(fs.job_dir):
        meta = fs.load_json("metadata/waveform_peaks.json") or {}
        response = send_file(bin_path, mimetype="application/octet-stream", conditional=True)
        response.headers["X-Peaks-Meta"] = json.dumps(meta)
        return response
    audio = _audio_path(fs)
    if audio is None:
        return jsonify({"error": "Audio non disponible — pas de forme d'onde."}), 404
    with _PEAKS_LOCK:
        if job_id not in _PEAKS_RUNNING:
            _PEAKS_RUNNING.add(job_id)
            threading.Thread(
                target=_generate_peaks_job, args=(audio, fs.job_dir, job_id),
                name=f"peaks-{job_id[:8]}", daemon=True,
            ).start()
    return jsonify({"status": "generating"}), 202


def _generate_peaks_job(audio: Path, job_dir: Path, job_id: str) -> None:
    try:
        generate_peaks(audio, job_dir)
    finally:
        with _PEAKS_LOCK:
            _PEAKS_RUNNING.discard(job_id)

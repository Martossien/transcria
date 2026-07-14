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
from transcria.web.job_access import get_job_for_api
from transcria.web.refine_shared import refine_running
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
    return get_job_for_api(job_id)


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


def _client_int(value: object, default: int = 0) -> int:
    """Entier issu d'un payload client, tolérant : une valeur absente ou non entière
    (ex. ``{"revision": "abc"}``) retombe sur ``default`` au lieu de lever un 500."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


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
    client_revision = _client_int(data.get("revision"))
    server_revision = _client_int(existing.get("revision"))
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

    # Reconstruction du package (best-effort, comme le changement d'options de rendu) :
    # sans elle, le ZIP servi contiendrait un DOCX antérieur aux corrections.
    try:
        from transcria.exports.package_builder import PackageBuilder

        PackageBuilder(get_config()).build_package(job)
    except Exception:
        logger.warning("Éditeur SRT : reconstruction du package échouée (best-effort) — job=%s",
                       job.id, exc_info=True)

    edited = _client_int(data.get("edited_count"))
    new_speakers_count = len(data.get("new_speakers") or [])
    audit_log(AuditAction.JOB_SRT_EDIT_SAVE, target_type="job", target_id=job.id,
              target_label=job.title,
              details={"version": version, "chunks": len(chunks), "edited": edited,
                       "new_speakers": new_speakers_count})
    warnings = validate_chunks(chunks, audio_duration_ms=_audio_duration_ms(fs))
    # Le verbatim/les stats du DOCX suivent le SRT automatiquement (régénéré au
    # téléchargement) ; la SYNTHÈSE, elle, ne se resynchronise que par une passe
    # LLM — on la PROPOSE (jamais automatique) dès qu'un contenu a changé.
    suggest = bool(edited or new_speakers_count) and _sync_summary_available(job)
    return jsonify({
        "version": version,
        "warnings": warnings,
        "summary_update_suggested": suggest,
        "edited_count": edited,
        "new_speakers_count": new_speakers_count,
    })


_SYNC_READY_STATES = ("completed", "export_ready")  # mêmes états que le chat d'affinage


def _sync_summary_available(job) -> bool:
    """La passe de resynchronisation est-elle proposable ? (mêmes prérequis que
    le chat d'affinage : job terminé + LLM d'arbitrage non désactivée)."""
    cfg = get_config()
    wf = cfg.get("workflow", {}) or {}
    if (wf.get("refine_chat", {}) or {}).get("enabled", True) is False:
        return False
    if (wf.get("arbitration_llm", {}) or {}).get("enabled") is False:
        return False
    return job.state in _SYNC_READY_STATES


def _sync_summary_message(language: str, edited: int, new_speakers: int) -> str:
    """Demande d'affinage composée côté serveur (tour utilisateur `apply`).

    Instruction ABSTRAITE (aucun contenu réel — l'agent lit lui-même le SRT
    corrigé et la synthèse dans son espace de travail) : mettre à jour UNIQUEMENT
    ce que le verbatim corrigé contredit, ne rien restructurer, signaler les
    ambiguïtés plutôt que trancher."""
    if language == "en":
        return (
            f"The user has just corrected the transcript in the SRT editor "
            f"({edited} segment(s) edited, {new_speakers} speaker(s) renamed or added). "
            "Update the summary and the structured data ONLY where the corrected "
            "transcript contradicts them: names, figures, speaker attributions, "
            "decisions and actions. Do not restructure or rewrite the style. "
            "If a point is ambiguous, flag it in your report instead of deciding."
        )
    return (
        f"L'utilisateur vient de corriger le verbatim dans l'éditeur SRT "
        f"({edited} segment(s) modifié(s), {new_speakers} locuteur(s) renommé(s) ou ajouté(s)). "
        "Mets à jour le résumé et les données structurées UNIQUEMENT là où le verbatim "
        "corrigé les contredit : noms, chiffres, attributions de parole, décisions et "
        "actions. Ne restructure rien, ne réécris pas le style. En cas d'ambiguïté, "
        "signale le point dans ton rapport au lieu de trancher."
    )


@editor_bp.route("/api/jobs/<job_id>/editor/sync-summary", methods=["POST"])
@login_required
def editor_sync_summary(job_id: str):
    """Enfile la passe LLM « synthèse mise à jour depuis le SRT corrigé ».

    Réutilise INTÉGRALEMENT la phase d'affinage (mode apply) : workspace agent,
    garde-fous déterministes, snapshot de version AVANT write-back, reconstruction
    du package. Le web ne fait que composer la demande et enfiler — jamais
    automatique, toujours au clic de l'utilisateur."""
    job, error = _get_job(job_id)
    if error:
        return error
    if not _sync_summary_available(job):
        return jsonify({"error": "Mise à jour de la synthèse indisponible "
                                 "(job non terminé ou LLM désactivée)."}), 409

    cfg = get_config()
    store = RefineStore(jobs_dir=cfg["storage"]["jobs_dir"], job_id=job.id)

    if store.has_active_request() or refine_running(job):
        return jsonify({"error": "Une demande d'affinage est déjà en cours pour ce job."}), 409

    from transcria.services.job_executor import REFINE_MODE, get_job_executor

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    from transcria.gpu.opencode_runner import resolve_output_language

    data = request.get_json(silent=True) or {}
    edited = _client_int(data.get("edited_count"))
    new_speakers = _client_int(data.get("new_speakers_count"))
    message = _sync_summary_message(resolve_output_language(job), edited, new_speakers)
    store.write_request(kind="apply", message=message)

    fs = _fs(job.id)
    audio_path = fs.get_original_audio_path()
    submit = executor.submit_process(job.id, str(audio_path or ""), REFINE_MODE)
    if not submit.get("accepted", True):
        store.consume_request()  # pas de demande fantôme
        return jsonify({"error": "Le job est déjà dans la file de traitement"}), 409

    audit_log(AuditAction.JOB_REFINE_REQUEST, target_type="job", target_id=job.id,
              target_label=job.title,
              details={"kind": "sync_summary", "edited": edited, "new_speakers": new_speakers})
    return jsonify({"accepted": True}), 202


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

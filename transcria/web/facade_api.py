"""Façade STT keystone (Phase K — temps réel & connecteurs de réunion).

Deux endpoints, tous deux OPT-IN derrière ``live.facade.enabled`` (défaut OFF →
routes absentes, surface d'API par défaut inchangée) et authentifiés par jeton
d'API personnel (``Authorization: Bearer tia_…``, chantier identité lot 4) :

- ``POST /v1/audio/transcriptions`` — transcription STT *sans état*, compatible
  *OpenAI Audio Transcriptions* (formats ``json``/``verbose_json``/``text``/``srt``).
  C'est la brique que les connecteurs de réunion et le micro direct pointent par
  URL (cf. docs/TEMPS_REEL_REUNIONS.md). Ses segments portent la provenance
  ``final_live`` (couture 1) : un suivi live, pas le document de référence.

- ``POST /v1/audio/ingest`` — dépôt d'un enregistrement post-réunion : crée un
  job TranscrIA et lance le pipeline complet (segments ``canonical``). Réutilise
  les MÊMES primitives que le wizard (``JobService`` + exécuteur), via l'API de
  jobs — jamais d'accès direct au pipeline. L'idempotence par
  ``external_meeting_id`` et la récupération par URL relèvent de la Phase 1
  (contrat ``MeetingProvider``) et sont ici seulement acceptées/tracées.

Ce module respecte ``routes-independantes`` (import-linter) : il n'importe aucun
autre module de routes, seulement des helpers partagés et les couches métier.
"""
import functools
import logging
import os
import tempfile
from pathlib import Path

from flask import Response, jsonify, request
from flask_login import current_user

from transcria.audio.analyzer import AudioAnalyzer
from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, get_user_permissions
from transcria.config import get_config
from transcria.ingestion.store import MeetingImportStore, compute_dedup_key
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.services.job_executor import get_job_executor
from transcria.services.job_service import JobService
from transcria.services.pipeline_service import PipelineService
from transcria.stt.provenance import FINAL_LIVE, stamp_provenance
from transcria.stt.transcriber_factory import create_transcriber, live_backend, summary_backend
from transcria.web import facade_format
from transcria.web.blueprint import web_bp
from transcria.web.request_helpers import bearer_token_required, clean_job_title
from transcria.workflow import profiles

logger = logging.getLogger(__name__)


def facade_enabled(view):
    """Garde OPT-IN : 404 tant que ``live.facade.enabled`` est faux.

    À poser en PREMIER (décorateur le plus externe) : si la façade est désactivée,
    l'endpoint se comporte comme s'il n'existait pas, AVANT toute authentification.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        cfg = get_config()
        if not (cfg.get("live", {}) or {}).get("facade", {}).get("enabled", False):
            return jsonify({"error": "Façade temps réel désactivée (live.facade.enabled)"}), 404
        return view(*args, **kwargs)

    return wrapper


def _resolve_facade_backend(cfg: dict) -> str:
    """Backend de la façade : la chaîne live si configurée, sinon le backend rapide
    du résumé (qui retombe lui-même sur le backend principal). Toujours une chaîne."""
    return live_backend(cfg) or summary_backend(cfg)


@web_bp.route("/v1/audio/transcriptions", methods=["POST"])
@facade_enabled
@bearer_token_required
def facade_transcriptions():
    """Transcription STT sans état, compatible OpenAI Audio Transcriptions."""
    cfg = get_config()
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier fourni (champ 'file')"}), 400

    # Garde-fou : l'inférence est SYNCHRONE (elle occupe un worker gunicorn sync). On
    # borne l'endpoint aux extraits COURTS ; un enregistrement complet doit passer par
    # /v1/audio/ingest (asynchrone). La TAILLE est un garde grossier (lue sans charger le
    # flux) : nécessaire mais insuffisant — un conteneur opus de 25 Mo décode des heures.
    # Le VRAI garde est la DURÉE (sondée plus bas, avant l'inférence).
    facade_cfg = cfg.get("live", {}).get("facade", {})
    max_mb = int(facade_cfg.get("max_sync_audio_mb", 25))
    file.stream.seek(0, os.SEEK_END)
    size_bytes = file.stream.tell()
    file.stream.seek(0)
    if size_bytes > max_mb * 1024 * 1024:
        return jsonify({
            "error": f"Fichier trop volumineux pour la transcription synchrone "
                     f"({size_bytes // (1024 * 1024)} Mo > {max_mb} Mo). "
                     f"Utilisez POST /v1/audio/ingest (asynchrone) pour un enregistrement complet.",
        }), 413

    response_format = request.form.get("response_format") or facade_format.DEFAULT_RESPONSE_FORMAT
    if response_format not in facade_format.RESPONSE_FORMATS:
        return jsonify({
            "error": f"response_format invalide: {response_format} "
                     f"(attendu: {', '.join(facade_format.RESPONSE_FORMATS)})"
        }), 400
    language = request.form.get("language") or "fr"
    backend = _resolve_facade_backend(cfg)

    # Fichier temporaire : les backends STT chargent + rééchantillonnent eux-mêmes
    # (librosa 16 k mono), on leur passe donc l'upload brut tel quel.
    tmp = tempfile.NamedTemporaryFile(
        prefix="facade_stt_", suffix=Path(file.filename).suffix.lower(), delete=False
    )
    try:
        tmp.write(file.read())
        tmp.close()
        # Garde de DURÉE (le vrai) : sonde ffprobe avant de lancer l'inférence synchrone.
        # Best-effort : si ffprobe échoue (format inconnu → duration 0), on laisse passer
        # (la borne de taille s'applique déjà) plutôt que rejeter un audio pourtant valide.
        max_duration_s = int(facade_cfg.get("max_sync_duration_s", 600))
        duration_s = float(AudioAnalyzer.analyze(Path(tmp.name)).get("duration_seconds") or 0.0)
        if duration_s > max_duration_s:
            return jsonify({
                "error": f"Enregistrement trop long pour la transcription synchrone "
                         f"({int(duration_s)} s > {max_duration_s} s). "
                         f"Utilisez POST /v1/audio/ingest (asynchrone) pour un enregistrement complet.",
            }), 413
        # Nœud de calcul configuré → l'inférence part LÀ-BAS (VRAM et GPU du bon nœud) ;
        # sinon inférence locale, comportement historique inchangé.
        inference_url = (facade_cfg.get("inference_url") or "").strip()
        if inference_url:
            segments = _remote_transcribe(inference_url, Path(tmp.name), language, backend,
                                          float(facade_cfg.get("inference_timeout_s", 300)))
            transcriber = None
        else:
            transcriber = create_transcriber(cfg, backend=backend)
            segments = transcriber.transcribe(Path(tmp.name), language=language)
    except Exception:  # noqa: BLE001 — moteur indispo/échec → 503 propre, pas un 500 opaque
        logger.exception("[façade] Transcription échouée (backend=%s)", backend)
        return jsonify({"error": "Moteur STT indisponible ou transcription échouée"}), 503
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # Provenance : sortie de la chaîne live = suivi, jamais la référence (couture 1).
    stamp_provenance(segments, FINAL_LIVE)

    if response_format == "verbose_json":
        return jsonify(facade_format.verbose_json(segments, language))
    if response_format == "json":
        return jsonify(facade_format.simple_json(segments))
    if response_format == "srt":
        # En délégation, aucun objet transcriber local : le rendu SRT est pur (segments dict).
        srt = (transcriber.segments_to_srt(segments) if transcriber is not None
               else facade_format.segments_to_srt(segments))
        return Response(srt, mimetype="application/x-subrip")
    return Response(facade_format.full_text(segments), mimetype="text/plain; charset=utf-8")


def _remote_transcribe(inference_url: str, audio_path: Path, language: str,
                       backend: str | None, timeout_s: float) -> list[dict]:
    """Délègue la transcription au NŒUD DE CALCUL (`POST /infer/transcribe`).

    Sans ça, la façade charge le modèle DANS LE PROCESS WEB : la VRAM de la frontale reste
    occupée au détriment de la file, et une frontale `role=web` sans GPU ne peut pas
    répondre du tout. Configuré par `live.facade.inference_url` — absent = comportement
    historique (inférence locale), donc aucune régression pour l'existant.
    """
    import requests

    with audio_path.open("rb") as handle:
        resp = requests.post(
            f"{inference_url.rstrip('/')}/infer/transcribe",
            files={"file": (audio_path.name, handle)},
            data={"language": language, **({"backend": backend} if backend else {})},
            timeout=timeout_s,
        )
    resp.raise_for_status()
    payload = resp.json() or {}
    return [seg for seg in (payload.get("segments") or []) if isinstance(seg, dict)]



def _create_and_queue_job(cfg: dict, file, title: str, *,
                          processing_profile_id: str | None = None,
                          requested_mode: str | None = None):
    """Crée → dépose → analyse → met en file un job depuis un fichier uploadé.

    Primitives identiques au wizard (JobService + exécuteur), via l'API de jobs.
    Retourne `(job_id, result, error)` : sur succès `result={job_id, profile_id, mode}`
    et `error=None` ; sur échec `error=(réponse JSON, code)` (et `job_id` renseigné dès
    qu'un job a été créé, pour le contexte d'erreur et la libération d'idempotence).

    Le profil/mode est CHOISI PAR L'APPELANT : une réunion à plusieurs intervenants doit
    pouvoir demander un profil qui DIARISE (`quality`), sans quoi le compte rendu ne sépare
    pas les personnes partageant un même micro (salle de réunion). Défaut `fast` inchangé.
    """
    try:
        profile, mode = profiles.resolve_request(processing_profile_id,
                                                 requested_mode or "fast")
    except (KeyError, ValueError) as exc:
        return None, None, (jsonify({"error": f"Profil ou mode inconnu: {exc}"}), 400)

    job_id = JobService.create(owner_id=current_user.id, title=title)["job_id"]
    JobService.upload(job_id, file.read(), file.filename, cfg["storage"]["jobs_dir"])

    analysis = JobService.analyze(job_id, cfg["storage"]["jobs_dir"], cfg)
    if analysis.get("error"):
        return job_id, None, (jsonify({"error": f"Analyse impossible: {analysis['error']}",
                                       "job_id": job_id}), 422)

    audio_path = JobFilesystem(cfg["storage"]["jobs_dir"], job_id).get_original_audio_path()
    if audio_path is None:
        return job_id, None, (jsonify({"error": "Fichier audio introuvable après dépôt",
                                       "job_id": job_id}), 500)

    executor = get_job_executor()
    if executor is None:
        return job_id, None, (jsonify({"error": "Worker de traitement indisponible",
                                       "job_id": job_id}), 503)

    vram_profile = PipelineService.estimate_profile_resources(cfg, profile)
    # Durée audio portée par l'entrée de file (comme la voie wizard) : en déploiement SPLIT,
    # la page File n'accède pas aux fichiers du job — sans ça l'estimation d'attente est vide.
    try:
        analysis_meta = JobFilesystem(cfg["storage"]["jobs_dir"], job_id).load_json(
            "metadata/audio_analysis.json") or {}
        vram_profile["audio_seconds"] = float(analysis_meta.get("duration_seconds") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort, l'attente retombe sur le fichier sinon
        pass
    try:
        result = executor.submit_process(
            job_id, str(audio_path), mode,
            vram_profile=vram_profile, processing_profile_id=profile.id,
        )
    except TypeError as exc:  # exécuteur plus ancien sans kwargs — repli identique à api_process
        if "unexpected keyword argument" not in str(exc):
            raise
        result = executor.submit_process(job_id, str(audio_path), mode)
    if not result.get("accepted"):
        return job_id, None, (jsonify({"error": "Un traitement est déjà en cours",
                                       "job_id": job_id}), 409)

    JobStore.update(job_id, processing_mode=mode)
    JobStore.update_state(job_id, JobState.READY_TO_PROCESS)
    return job_id, {"job_id": job_id, "profile_id": profile.id, "mode": mode}, None


@web_bp.route("/v1/audio/ingest", methods=["POST"])
@facade_enabled
@bearer_token_required
def facade_ingest():
    """Dépôt d'un enregistrement post-réunion → job TranscrIA (pipeline complet).

    Idempotence côté serveur (ADR-001 D2) : un en-tête `Idempotency-Key` (le
    connecteur y met sa clé composite provider+compte+occurrence+artefact) déduplique
    l'import via `MeetingImport` — un rejeu renvoie le MÊME job, jamais un second.
    """
    cfg = get_config()
    # Permission vérifiée en JSON (la façade /v1 n'est pas sous /api : un abort()
    # rendrait une page HTML — inadapté à un client machine).
    if Permission.CREATE_JOBS not in get_user_permissions(current_user):
        return jsonify({"error": "Permission requise: création de jobs"}), 403
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier fourni (champ 'file')"}), 400

    ext = Path(file.filename).suffix.lower()
    allowed = cfg.get("security", {}).get("allowed_upload_extensions", [".mp3", ".wav"])
    if ext not in allowed:
        return jsonify({"error": f"Format non supporté: {ext}"}), 400

    external_meeting_id = (request.form.get("external_meeting_id") or "").strip() or None
    provider = (request.form.get("provider") or "").strip() or None
    title = clean_job_title(request.form.get("title") or Path(file.filename).stem)

    # Idempotence : si une clé est fournie, on RÉSERVE l'import avant de créer le job.
    idem = (request.headers.get("Idempotency-Key") or "").strip()
    dedup_key = compute_dedup_key(idem) if idem else None
    if dedup_key:
        record, created = MeetingImportStore.get_or_create(
            dedup_key, provider=provider, external_occurrence_id=external_meeting_id)
        if not created:
            # Doublon / rejeu / course perdue : on NE crée PAS un second job.
            if record.job_id:
                return jsonify({"job_id": record.job_id, "idempotent": True,
                                "status_url": f"/api/jobs/{record.job_id}/status"}), 200
            return jsonify({"status": "import_in_progress", "import_id": record.id}), 202

    # Réunion à plusieurs intervenants : l'appelant peut demander un profil qui DIARISE
    # (`mode=quality`), indispensable pour séparer les personnes d'une même salle.
    job_id, result, error = _create_and_queue_job(
        cfg, file, title,
        processing_profile_id=(request.form.get("processing_profile_id") or "").strip() or None,
        requested_mode=(request.form.get("mode") or "").strip() or None)
    if error is not None:
        if dedup_key:
            MeetingImportStore.release(dedup_key)  # rejeu propre après échec
        return error
    if dedup_key:
        MeetingImportStore.attach_job(dedup_key, result["job_id"])

    audit_log(
        action=AuditAction.JOB_ENQUEUE,
        target_type="job",
        target_id=result["job_id"],
        target_label=title,
        details={
            "source": "facade_ingest",
            "provider": provider,
            "external_meeting_id": external_meeting_id,
            "idempotent": bool(dedup_key),
            "processing_profile_id": result["profile_id"],
            "mode": result["mode"],
        },
    )
    return jsonify({
        "job_id": result["job_id"],
        "state": JobState.READY_TO_PROCESS.value,
        "processing_profile_id": result["profile_id"],
        "mode": result["mode"],
        "external_meeting_id": external_meeting_id,
        "status_url": f"/api/jobs/{result['job_id']}/status",
    }), 202

"""API JSON du parcours de création (wizard) : upload → analyse → résumé → contexte
→ participants → locuteurs → profil.

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py`` ;
imports remontés en tête en vague C5 (le boot du serveur charge déjà l'orchestration).
"""
import logging
import threading
from pathlib import Path

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.context.document_extractor import (
    DocumentExtractionError,
    extract_document_text,
)
from transcria.context.invite_parser import sanitize_invite
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.meeting_context import MeetingContextManager
from transcria.context.meeting_type_catalog import meeting_type_names
from transcria.context.meeting_type_store import MeetingTypeStore
from transcria.context.participants import ParticipantsManager
from transcria.gpu import llm_prelaunch
from transcria.i18n import select_locale
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore

# Accès PAR MODULE : les tests substituent alert_admin_vram_wait à la source.
from transcria.notifications import admin_alerts
from transcria.queue.store import QUEUE_PAUSED, QUEUE_RUNNING, QUEUE_WAITING, QueueStore
from transcria.services.job_executor import SPEAKER_MODE, SUMMARY_MODE, get_job_executor
from transcria.services.job_service import JobService
from transcria.stt.speaker_detection import SpeakerDetector
from transcria.stt.transcriber_factory import get_backend_vram_mb, summary_backend
from transcria.voice.matching import VoiceMatchingService
from transcria.web.blueprint import web_bp
from transcria.web.job_access import get_job_for_api
from transcria.web.request_helpers import DEFAULT_JOB_TITLE, api_stable, clean_job_title, json_body
from transcria.workflow import profiles, resume
from transcria.workflow.profile_availability import compute_profiles_view
from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.transitions import (
    advance_preprocessing_state,
    mark_execution_waiting_vram,
    set_processing_profile,
)

logger = logging.getLogger(__name__)


_VRAM_WAIT_MESSAGE = (
    "VRAM insuffisante : l'administrateur a été prévenu. "
    "Le résumé reprendra automatiquement dès que la mémoire GPU sera libérée."
)

_SUMMARY_QUEUED_MESSAGE = (
    "Résumé mis en file sur le nœud GPU — il s'exécutera automatiquement "
    "et la page se rafraîchira dès qu'il sera prêt."
)

_SPEAKER_QUEUED_MESSAGE = (
    "Détection des locuteurs lancée sur le nœud GPU — elle s'exécutera automatiquement "
    "et la page se rafraîchira dès qu'elle sera prête."
)

# Production vide : opencode a tourné (exit 0) mais n'a émis aucun texte → piste
# transcript/modèle/prompt.
_SUMMARY_LLM_FAILED_MESSAGE = (
    "Le résumé n'a pas pu être généré : la LLM d'arbitrage a répondu mais n'a produit "
    "aucun texte après 3 tentatives (cause fréquente : transcript trop long, modèle ou prompt). "
    "La transcription rapide est conservée — vous pouvez relancer le résumé "
    "(diagnostic : transcria doctor --llm-smoke)."
)

# Erreur opencode : la LLM n'a pas pu être appelée correctement (modèle non résolu côté
# opencode, serveur en erreur, binaire absent…) → PAS un problème de transcript ; le
# diagnostic statique « transcria doctor » pointe la vraie cause (résolution du modèle).
_SUMMARY_LLM_UNREACHABLE_MESSAGE = (
    "Le résumé n'a pas pu être généré : la LLM d'arbitrage n'a pas pu être appelée "
    "correctement (modèle non résolu côté opencode, ou serveur en erreur) — ce n'est pas "
    "un problème de transcript. La transcription rapide est conservée. "
    "Diagnostic : transcria doctor (vérifie la résolution du modèle opencode et le serveur ; "
    "réaligner avec scripts/setup_opencode.py si besoin)."
)


def _speaker_vram_profile(cfg: dict) -> dict:
    """Profil VRAM d'une détection de locuteurs (pyannote) routée vers le worker."""
    pyannote = int(cfg.get("gpu", {}).get("pyannote_vram_mb", 2000))
    return {"mode": "speakers", "peak_vram_mb": pyannote, "phases": {"speaker_detection": pyannote}}


def _summary_vram_profile(cfg: dict) -> dict:
    """Profil VRAM d'une reprise serveur du résumé : pilote l'admission du scheduler.

    Le résumé ne charge que le STT rapide ; l'admission ne dispatchera l'entrée que
    lorsque cette VRAM est réellement libre (sinon le job patiente en file).
    """

    backend = summary_backend(cfg)
    summary_vram = int(get_backend_vram_mb(backend, cfg))
    return {
        "mode": "summary",
        "peak_vram_mb": summary_vram,
        "phases": {"summary_stt": summary_vram},
    }


@web_bp.route("/api/jobs/<job_id>/upload", methods=["POST"])
@login_required
@api_stable
def api_upload(job_id: str):
    """Dépose le fichier audio d'un job fraîchement créé (contrat scriptable)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    if job.state != JobState.CREATED.value:
        return jsonify({"error": "Ce job a déjà un fichier ou a déjà démarré"}), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    ext = Path(file.filename).suffix.lower()
    allowed = cfg.get("security", {}).get("allowed_upload_extensions", [".mp3", ".wav"])
    if ext not in allowed:
        return jsonify({"error": f"Format non supporté: {ext}"}), 400

    info = JobService.upload(job.id, file.read(), file.filename, cfg["storage"]["jobs_dir"])
    if job.title == DEFAULT_JOB_TITLE:
        job.title = clean_job_title(Path(file.filename).stem or file.filename)
    # §5.6 (opt-in) : enchaîner analyse → mise en file du résumé pendant que
    # l'utilisateur remplit le wizard — l'attente perçue de l'étape résumé fond.
    _maybe_autostart_summary(cfg, job.id)
    return jsonify(info)


def _maybe_autostart_summary(cfg: dict, job_id: str) -> None:
    """Autostart du résumé dès la fin de l'upload (PISTES_AMELIORATION §5.6).

    Opt-in `workflow.summary_autostart.enabled` (défaut false = comportement
    historique). Enchaîne en THREAD : analyse (CPU, quelques secondes — le résumé
    a besoin de ses artefacts pour le VAD adaptatif) puis mise en FILE du résumé
    (SUMMARY_MODE) — le même véhicule que la reprise serveur : admission VRAM par
    le scheduler, exécution locale en all-in-one, par le worker GPU en frontal.
    Best-effort : n'affecte jamais la réponse d'upload ; toute étape déjà faite
    ou en cours est respectée (mêmes gardes qu'api_summary).
    """
    autostart_cfg = cfg.get("workflow", {}).get("summary_autostart") or {}
    if not autostart_cfg.get("enabled", False):
        return
    # Patron Flask canonique : current_app est un LocalProxy, _get_current_object
    # rend la vraie app pour le thread de fond.
    app_obj = current_app._get_current_object()  # type: ignore[attr-defined]

    def _run() -> None:
        try:
            with app_obj.app_context():
                job = JobStore.get_by_id(job_id)
                if job is None or job.state != JobState.UPLOADED.value:
                    return  # supprimé, ou déjà analysé/résumé par ailleurs
                result = JobService.analyze(job_id, cfg["storage"]["jobs_dir"], cfg)
                if result.get("error"):
                    logger.warning("[autostart] Analyse impossible — résumé non enfilé (job=%s : %s)",
                                   job_id, result["error"])
                    return
                job = JobStore.get_by_id(job_id)
                if job is None or job.state != JobState.ANALYZED.value:
                    return
                pending = QueueStore.get_entry(job_id)
                if (pending is not None and pending.mode == SUMMARY_MODE
                        and pending.status in {QUEUE_WAITING, QUEUE_RUNNING, QUEUE_PAUSED}):
                    return  # déjà en file (double upload rapide, reprise…)
                fs = JobFilesystem(cfg["storage"]["jobs_dir"], job_id)
                audio_path = fs.get_original_audio_path()
                executor = get_job_executor()
                if audio_path is None or executor is None:
                    return
                executor.submit_process(
                    job_id, str(audio_path), SUMMARY_MODE,
                    vram_profile=_summary_vram_profile(cfg),
                )
                logger.info("[autostart] Résumé enfilé dès l'upload (job=%s)", job_id)
        except Exception:  # noqa: BLE001 — opportuniste, jamais bloquant pour l'upload
            logger.debug("[autostart] Autostart du résumé abandonné (job=%s)", job_id, exc_info=True)

    threading.Thread(target=_run, name="summary-autostart", daemon=True).start()


@web_bp.route("/api/jobs/<job_id>/analyze", methods=["POST"])
@login_required
def api_analyze(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    # Garde d'état (§5.6) : au-delà d'ANALYZED (résumé en cours/fait — ex. lancé par
    # l'autostart), re-analyser RÉGRESSERAIT l'état du wizard (JobService.analyze pose
    # ANALYZED sans condition). On renvoie l'analyse déjà calculée, idempotent.
    if job.state not in (JobState.UPLOADED.value, JobState.ANALYZED.value):
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        stored = fs.load_json("metadata/audio_analysis.json")
        if stored is not None:
            return jsonify(stored)

    result = JobService.analyze(job.id, cfg["storage"]["jobs_dir"], cfg)
    if result.get("error"):
        return jsonify(result), 400
    # Pré-lancement opt-in de la LLM d'arbitrage (lot 2, §4.3-4) : détail et
    # discipline de verrou dans gpu/llm_prelaunch.py — best-effort, thread.
    llm_prelaunch.maybe_prelaunch_arbitrage_llm(cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/summary", methods=["POST"])
@login_required
def api_summary(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    # run_summary est synchrone dans la requête HTTP et reste SUMMARY_RUNNING pendant
    # toute sa durée (STT rapide → scène → pyannote → LLM). Refuser un second appel
    # concurrent évite deux pipelines GPU simultanés et la corruption de meeting_context.json.
    if job.state == JobState.SUMMARY_RUNNING.value:
        return jsonify({"error": "Un résumé est déjà en cours pour ce job."}), 409

    # Une reprise serveur du résumé peut déjà être en file (après une attente de VRAM) :
    # ne PAS relancer en synchrone (sinon deux run_summary concurrents). Le client doit
    # poller GET /status — le scheduler reprend dès que la VRAM se libère.
    pending = QueueStore.get_entry(job.id)
    if (
        pending is not None
        and pending.mode == SUMMARY_MODE
        and pending.status in {QUEUE_WAITING, QUEUE_RUNNING, QUEUE_PAUSED}
    ):
        return jsonify({"queued": True, "message": _SUMMARY_QUEUED_MESSAGE})

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    # Topologie split : le frontal (`role=web`) n'a pas (forcément) de GPU et ne doit
    # JAMAIS exécuter de phase GPU. On enfile le résumé sur le worker GPU (nœud de
    # ressources), qui exécute STT/diarisation/LLM ; le client poll GET /status. La
    # décision se fait sur le RÔLE, pas sur une détection matérielle (un éventuel petit
    # GPU frontal est volontairement ignoré). En `all-in-one`, exécution synchrone.
    if current_app.config.get("TRANSCRIA_ROLE", "all") == "web":
        executor = get_job_executor()
        if executor is None:
            return jsonify({"error": "Worker de traitement indisponible"}), 503
        executor.submit_process(
            job.id, str(audio_path), SUMMARY_MODE, vram_profile=_summary_vram_profile(cfg)
        )
        return jsonify({"queued": True, "message": _SUMMARY_QUEUED_MESSAGE})

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_summary(job, str(audio_path), cfg)

    if result.get("vram_wait"):
        # VRAM momentanément insuffisante : le job N'A PAS échoué (run_summary a déjà
        # restauré son état pré-résumé). On le marque « en attente de VRAM », on alerte
        # l'admin une seule fois, puis on enfile une reprise SERVEUR (mode `summary`) :
        # le scheduler relancera run_summary dès que la VRAM le permet, même sans page
        # ouverte. Le client n'a plus qu'à poller GET /status.
        required_mb = int(result.get("required_mb") or 0)
        phase = result.get("phase") or "summary_stt"

        # Enfiler la reprise serveur d'abord (submit_process pose le statut « queued »),
        # PUIS marquer « waiting_vram » : le statut final reflète l'attente de VRAM (et
        # alimente le bandeau admin), tandis que l'entrée de file pilote la reprise.
        executor = get_job_executor()
        if executor is not None:
            executor.submit_process(
                job.id,
                str(audio_path),
                SUMMARY_MODE,
                vram_profile=_summary_vram_profile(cfg),
            )
        else:
            logger.warning("Reprise serveur du résumé indisponible (worker absent) — job=%s", job.id)

        first_wait = mark_execution_waiting_vram(job.id, required_mb=required_mb, phase=phase)
        if first_wait:
            admin_alerts.alert_admin_vram_wait(cfg, job, required_mb=required_mb, phase=phase)
        return jsonify({
            "vram_wait": True,
            "queued": executor is not None,
            "required_mb": required_mb,
            "phase": phase,
            "message": _VRAM_WAIT_MESSAGE,
        })

    if result.get("summary_llm_failed"):
        # La LLM d'arbitrage n'a rien produit après 3 tentatives : le résumé n'est PAS
        # validé (meeting_context non corrompu, job non SUMMARY_DONE), mais relançable.
        # Le message dépend de la CLASSE de panne (corrections opposées) : erreur opencode
        # (modèle non résolu / serveur) → doctor ; production vide → transcript/prompt.
        error_kind = result.get("summary_llm_error_kind", "empty_output")
        message = (_SUMMARY_LLM_UNREACHABLE_MESSAGE if error_kind == "opencode_error"
                   else _SUMMARY_LLM_FAILED_MESSAGE)
        return jsonify({
            "summary_llm_failed": True,
            "error_kind": error_kind,
            "attempts": 3,
            "message": message,
        })

    return jsonify(result)


def _normalize_speaker_hint(data: dict) -> dict:
    """Normalise la fourchette de locuteurs saisie : entiers 1..50, min <= max.

    Retourne ``{"min": int|None, "max": int|None}``. Une valeur vide ou hors plage
    devient ``None`` (aucune contrainte sur cette borne).
    """
    def _coerce(value) -> int | None:
        try:
            ival = int(value)
        except (TypeError, ValueError):
            return None
        return ival if 1 <= ival <= 50 else None

    vmin = _coerce(data.get("min"))
    vmax = _coerce(data.get("max"))
    if vmin is not None and vmax is not None and vmin > vmax:
        vmin, vmax = vmax, vmin
    return {"min": vmin, "max": vmax}


@web_bp.route("/api/jobs/<job_id>/speaker-hint", methods=["POST"])
@login_required
def api_speaker_hint(job_id: str):
    """Mémorise la fourchette de locuteurs (min/max) choisie par l'utilisateur.

    Indication facultative saisie après l'upload pour cadrer la diarisation (gain
    de temps pyannote, meilleur comptage) et basculer automatiquement de Sortformer
    vers pyannote si le maximum dépasse 4 locuteurs.
    """
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    body, err = json_body(dict)
    if err:
        return err
    hint = _normalize_speaker_hint(body)
    JobStore.update_extra_data(job.id, lambda extra: {**extra, "speaker_hint": hint})
    return jsonify({"status": "ok", "speaker_hint": hint})


@web_bp.route("/api/jobs/<job_id>/meeting-invite", methods=["POST"])
@login_required
def api_meeting_invite(job_id: str):
    """Mémorise une invitation de réunion collée (objet, corps, destinataires).

    Indication facultative, saisie avant la génération du résumé : elle fournit à
    la LLM d'arbitrage des indices d'orthographe des noms et de structure (ordre du
    jour, rôles). Le texte est nettoyé immédiatement : les adresses e-mail servent à
    dériver l'orthographe des noms puis sont retirées — seuls le brief sans e-mail et
    la liste de noms sont conservés (minimisation des données personnelles).
    """
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    body, err = json_body(dict)
    if err:
        return err
    raw = body.get("text", "")
    invite = sanitize_invite(raw if isinstance(raw, str) else "")

    def _merge(extra: dict) -> dict:
        # Préserver les documents joints (gérés par la route dédiée) : le texte collé
        # et les documents alimentent le même canal ``meeting_invite`` sans s'écraser.
        previous = extra.get("meeting_invite") or {}
        documents = previous.get("documents") if isinstance(previous, dict) else None
        merged = {**invite, "documents": documents} if documents else invite
        return {**extra, "meeting_invite": merged}

    updated = JobStore.update_extra_data(job.id, _merge)
    _invalidate_correction_on_invite_change(updated or job)
    return jsonify({"status": "ok", "names": invite["names"]})


def _document_summary(doc: dict) -> dict:
    """Vue légère d'un document joint pour l'UI (sans renvoyer tout le texte extrait)."""
    return {
        "name": doc.get("name", "document"),
        "format": doc.get("format", ""),
        "pages": doc.get("pages", 0),
        "slides": doc.get("slides", 0),
        "images_skipped": doc.get("images_skipped", 0),
        "chars": len((doc.get("text") or "")),
        "truncated": bool(doc.get("truncated")),
        "warnings": doc.get("warnings") or [],
    }


def _meeting_documents(job: Job) -> list[dict]:
    invite = (job.get_extra_data() or {}).get("meeting_invite") or {}
    docs = invite.get("documents") if isinstance(invite, dict) else None
    return [_document_summary(d) for d in docs if isinstance(d, dict)] if docs else []


def _invalidate_correction_on_invite_change(job: Job) -> None:
    """Le contexte d'invitation (texte + documents) alimente `meeting_invite.md`, lu par la
    phase de correction. La provenance de reprise ne trace que des FICHIERS
    (`_PHASE_INPUTS`), pas ce contenu dérivé de `extra_data` — un changement après une
    correction déjà faite serait donc ignoré à la re-exécution du pipeline. Si la
    correction est marquée faite, on l'invalide : la cascade de provenance rejoue ensuite
    final_review / quality / export via `transcription_corrigee.srt`. No-op sinon."""
    if job is not None and "correction" in resume.get_completed_phases(job):
        resume.unmark_phase(JobStore, job.id, "correction")


@web_bp.route("/api/jobs/<job_id>/meeting-invite/document", methods=["POST"])
@login_required
def api_meeting_invite_document(job_id: str):
    """Joint un document présenté (PDF/DOCX/PPTX/TXT) au contexte du résumé.

    Le texte est extrait immédiatement (images ignorées en v1), les e-mails sont
    retirés (minimisation PII) et **le binaire n'est jamais conservé** — seul le
    texte assaini alimente ``meeting_invite.documents``, dans le même canal que
    l'invitation collée. Les formats binaires hérités (.doc/.ppt) ne sont pas gérés.
    """
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    sec = cfg.get("security", {})
    allowed = sec.get("allowed_document_extensions", [".pdf", ".docx", ".pptx", ".txt"])
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        return jsonify({
            "error": f"Format non géré : {ext or file.filename}. "
                     f"Acceptés : {', '.join(allowed)} (convertissez les .doc/.ppt hérités)."
        }), 400

    # Borne le nombre de documents (donc le contexte LLM agrégé) — rejet précoce,
    # avant même de lire le fichier.
    max_docs = int(sec.get("max_documents_per_job", 15))
    if len(_meeting_documents(job)) >= max_docs:
        return jsonify({
            "error": f"Trop de documents joints (max {max_docs}). Retirez-en un avant d'ajouter."
        }), 400

    data = file.read()
    max_mb = int(sec.get("max_document_size_mb", 25))
    if len(data) > max_mb * 1024 * 1024:
        return jsonify({"error": f"Document trop volumineux (max {max_mb} Mo)."}), 400

    try:
        extracted = extract_document_text(
            data, file.filename, max_chars=int(sec.get("max_document_chars", 12000))
        )
    except DocumentExtractionError as exc:
        return jsonify({"error": str(exc)}), 400

    entry = {
        "name": Path(file.filename).name,
        "format": extracted.format,
        "pages": extracted.pages,
        "slides": extracted.slides,
        "images_skipped": extracted.images_skipped,
        "truncated": extracted.truncated,
        "warnings": extracted.warnings,
        "text": extracted.text,
    }

    def _add(extra: dict) -> dict:
        invite = extra.get("meeting_invite")
        invite = dict(invite) if isinstance(invite, dict) else {"brief": "", "names": []}
        documents = list(invite.get("documents") or [])
        documents.append(entry)
        invite["documents"] = documents
        return {**extra, "meeting_invite": invite}

    updated = JobStore.update_extra_data(job.id, _add)
    _invalidate_correction_on_invite_change(updated or job)
    return jsonify({"status": "ok", "documents": _meeting_documents(updated or job)})


@web_bp.route("/api/jobs/<job_id>/meeting-invite/document/<int:index>", methods=["DELETE"])
@login_required
def api_meeting_invite_document_delete(job_id: str, index: int):
    """Retire un document joint (par position dans la liste)."""
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    removed = False

    def _remove(extra: dict) -> dict:
        nonlocal removed
        invite = extra.get("meeting_invite")
        if not isinstance(invite, dict):
            return extra
        documents = list(invite.get("documents") or [])
        if 0 <= index < len(documents):
            documents.pop(index)
            removed = True
        invite = {**invite, "documents": documents}
        return {**extra, "meeting_invite": invite}

    updated = JobStore.update_extra_data(job.id, _remove)
    if not removed:
        return jsonify({"error": "Document introuvable"}), 404
    _invalidate_correction_on_invite_change(updated or job)
    return jsonify({"status": "ok", "documents": _meeting_documents(updated or job)})


@web_bp.route("/api/jobs/<job_id>/context", methods=["POST"])
@login_required
def api_context(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    data, _json_err = json_body(dict)
    if _json_err:
        return _json_err
    # Type de réunion : validé contre le catalogue visible du PROPRIÉTAIRE (intégrés +
    # personnalisés). Un type personnalisé est MATÉRIALISÉ dans le job (sa fiche complète,
    # sans binaire) : le rendu et le worker n'ont jamais à résoudre un template en base —
    # robuste en topologie split, et la suppression du template ne casse aucun job.
    if data.get("meeting_type"):
        chosen = str(data["meeting_type"])
        if chosen in meeting_type_names():
            data["custom_type"] = None
            stale_logo = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).job_dir / "context" / "type_logo.png"
            if stale_logo.exists():
                stale_logo.unlink()
        else:
            _owner = UserStore.get_by_id(job.owner_id)
            template = MeetingTypeStore.resolve_for_user(_owner, chosen) if _owner else None
            if template is None:
                return jsonify({"error": f"Type de réunion inconnu : {chosen}"}), 400
            data["custom_type"] = {**template.definition, "template_id": template.id}
            # Le logo (binaire, hors fiche) est matérialisé lui aussi : le rendu DOCX
            # le lit dans le job (context/ est un préfixe synchronisé en topologie pg).
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            logo_target = fs.job_dir / "context" / "type_logo.png"
            if template.logo_blob:
                logo_target.write_bytes(template.logo_blob)
            elif logo_target.exists():
                logo_target.unlink()
    merged_ctx = MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], data)
    # La langue des livrables est lue depuis extra_data (STT rapide, transcription, rapports,
    # DOCX via resolve_output_language). Le formulaire ne mettait à jour QUE le fichier
    # meeting_context.json → un choix EXPLICITE de langue était ignoré. On le reflète ici pour
    # qu'il prime sur le repli « locale du propriétaire ».
    _ctx_lang = (merged_ctx or {}).get("language")
    if _ctx_lang:
        JobStore.update_extra_data(
            job.id,
            lambda e: {**e, "meeting_context": {**(e.get("meeting_context") or {}), "language": _ctx_lang}},
        )
    audit_log(AuditAction.JOB_CONTEXT_SAVE, target_type="job", target_id=job.id, target_label=job.title)
    if job.state == JobState.SUMMARY_DONE.value:
        JobStore.update_state(job.id, JobState.CONTEXT_DONE)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/participants", methods=["POST"])
@login_required
def api_participants(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    data, _json_err = json_body(list)
    if _json_err:
        return _json_err
    ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], data)
    audit_log(AuditAction.JOB_PARTICIPANTS_SAVE, target_type="job", target_id=job.id, target_label=job.title)
    if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
        JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/speakers/detect", methods=["POST"])
@login_required
def api_speakers_detect(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    # run_speaker_detection est synchrone et publie SPEAKER_DETECTION_RUNNING le temps
    # de pyannote. Refuser un second appel concurrent évite deux runs GPU simultanés et
    # une course sur meeting_context.json (même classe que api_summary).
    if job.state == JobState.SPEAKER_DETECTION_RUNNING.value:
        return jsonify({"error": "Une détection des locuteurs est déjà en cours pour ce job."}), 409

    # Détection déjà en file sur le worker (frontal sans GPU) : ne pas relancer en synchrone.
    pending = QueueStore.get_entry(job.id)
    if (
        pending is not None
        and pending.mode == SPEAKER_MODE
        and pending.status in {QUEUE_WAITING, QUEUE_RUNNING, QUEUE_PAUSED}
    ):
        return jsonify({"queued": True, "message": _SPEAKER_QUEUED_MESSAGE})

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    # Frontal `role=web` (sans GPU) : la détection (pyannote) est une phase GPU → on la
    # délègue au worker GPU comme le résumé. Le client poll GET /status. En `all`, synchrone.
    if current_app.config.get("TRANSCRIA_ROLE", "all") == "web":
        executor = get_job_executor()
        if executor is None:
            return jsonify({"error": "Worker de traitement indisponible"}), 503
        executor.submit_process(
            job.id, str(audio_path), SPEAKER_MODE, vram_profile=_speaker_vram_profile(cfg)
        )
        return jsonify({"queued": True, "message": _SPEAKER_QUEUED_MESSAGE})

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_speaker_detection(job, str(audio_path), cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/speakers/map", methods=["POST"])
@login_required
def api_speakers_map(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    mapping, _json_err = json_body(dict)
    if _json_err:
        return _json_err

    SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])

    # Réappliquer les rôles LLM maintenant que le mapping SPEAKER_XX → participant existe
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    speaker_roles_llm = meeting_ctx.get("speaker_roles_llm", {})
    if speaker_roles_llm:
        WorkflowRunner._apply_speaker_roles(fs, speaker_roles_llm, logger)

    advance_preprocessing_state(job.id, job.state)
    audit_log(AuditAction.JOB_SPEAKER_MAP, target_type="job", target_id=job.id, target_label=job.title)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/speakers/voice-match", methods=["POST"])
@login_required
def api_speakers_voice_match(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    if not cfg.get("voice_enrollment", {}).get("enabled", False):
        return jsonify({"error": "Voix enregistrées désactivées dans la configuration."}), 400

    service = VoiceMatchingService(cfg, device="cpu")
    result = service.match_job_speakers(job, current_user)
    status = 200 if result.get("available") else 409
    return jsonify(result), status


@web_bp.route("/api/profiles/availability", methods=["GET"])
@login_required
def api_profiles_availability():
    """Profils de traitement disponibles + profil recommandé (source unique pour le wizard)."""
    return jsonify(compute_profiles_view(get_config(), select_locale()))


@web_bp.route("/api/jobs/<job_id>/profile", methods=["POST"])
@login_required
def api_set_profile(job_id: str):
    """Persiste le profil choisi à l'étape 1 (le wizard adapte alors ses étapes au profil).

    Distinct du lancement (`/process`) : ici on ne fait QUE mémoriser le contrat produit, sans
    enfiler le job. Le profil doit être valide ET réellement disponible sur cette installation.
    """
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {} if request.is_json else {}
    profile_id = payload.get("processing_profile_id") or request.form.get("processing_profile_id")
    if not profile_id or not profiles.is_profile(profile_id):
        return jsonify({"error": f"Profil de traitement invalide: {profile_id}"}), 400

    view = compute_profiles_view(get_config())
    available = {p["id"] for p in view["profiles"] if p["available"]}
    if profile_id not in available:
        return jsonify({"error": "Profil indisponible sur cette installation", "processing_profile_id": profile_id}), 409

    set_processing_profile(job.id, profile_id)
    return jsonify({"status": "ok", "processing_profile_id": profile_id})

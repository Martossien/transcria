import copy
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy import func

from transcria.audio.excerpts import AudioExcerptService, parse_time_range
from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, requires
from transcria.config import _deep_merge, get_config
from transcria.context.central_lexicon_service import merge_lexicon_entries, prefilter_lexicon_entries_for_display
from transcria.context.central_lexicon_store import CentralLexiconStore
from transcria.context.document_extractor import (
    DocumentExtractionError,
    extract_document_text,
)
from transcria.context.invite_parser import sanitize_invite
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.lexicon_audit import lexicon_entries_audit_summary, lexicon_text_audit_summary
from transcria.context.meeting_context import MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.database import db
from transcria.jobs import artifact_store
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.services.config_service import ConfigService
from transcria.services.job_executor import get_job_executor
from transcria.services.job_service import JobService
from transcria.web.config_form import (
    CONFIG_FORM_SECTIONS,
    build_partial_config,
    display_values,
    restore_masked_secrets,
)
from transcria.web.ui_labels import state_badge, state_label
from transcria.workflow.states import WorkflowState
from transcria.workflow.transitions import (
    advance_preprocessing_state,
    can_start_profile,
    get_execution_status,
    is_execution_active,
    mark_execution_cancelled,
    mark_execution_waiting_vram,
    request_execution_cancel,
)

web_bp = Blueprint("web", __name__)
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


def _effective_srt(fs) -> str | None:
    """SRT « livrable » : la version corrigée (correction LLM, affinage) prime sur le brut.

    Même préférence que ``/download/srt`` — les aperçus à l'écran et l'éditeur SRT
    doivent montrer ce que l'utilisateur téléchargera, pas la transcription brute.
    """
    return fs.load_text("metadata/transcription_corrigee.srt") or fs.load_text("metadata/transcription.srt")


def _speaker_vram_profile(cfg: dict) -> dict:
    """Profil VRAM d'une détection de locuteurs (pyannote) routée vers le worker."""
    pyannote = int(cfg.get("gpu", {}).get("pyannote_vram_mb", 2000))
    return {"mode": "speakers", "peak_vram_mb": pyannote, "phases": {"speaker_detection": pyannote}}


def _summary_vram_profile(cfg: dict) -> dict:
    """Profil VRAM d'une reprise serveur du résumé : pilote l'admission du scheduler.

    Le résumé ne charge que le STT rapide ; l'admission ne dispatchera l'entrée que
    lorsque cette VRAM est réellement libre (sinon le job patiente en file).
    """
    from transcria.stt.transcriber_factory import get_backend_vram_mb

    backend = cfg.get("models", {}).get("stt_backend", "cohere")
    summary_vram = int(get_backend_vram_mb(backend, cfg))
    return {
        "mode": "summary",
        "peak_vram_mb": summary_vram,
        "phases": {"summary_stt": summary_vram},
    }


@web_bp.app_template_filter("state_label")
def _state_label_filter(state):
    """Libellé français d'un état de job — aucun état brut à l'écran (REFONTE_UI)."""
    return state_label(state)


@web_bp.app_template_filter("state_badge")
def _state_badge_filter(state):
    return state_badge(state)


@web_bp.app_context_processor
def inject_vram_waiting_count():
    """Expose le nombre de jobs en attente de VRAM aux templates (bandeau admin).

    Calculé uniquement pour les administrateurs ; 0 sinon (aucun coût pour les autres).
    Best-effort : ne casse jamais le rendu.
    """
    try:
        if current_user and current_user.is_authenticated and current_user.has_role(Role.ADMIN):
            return {"vram_waiting_count": JobStore.count_waiting_vram()}
    except Exception:  # noqa: BLE001
        pass
    return {"vram_waiting_count": 0}

@web_bp.before_app_request
def _materialize_job_files():
    """Backend `pg` (split sans filesystem partagé) : matérialisation PARESSEUSE.

    Avant toute requête portant un `job_id`, rapatrie depuis la base les fichiers du job
    que ce tier n'a pas encore (artefacts écrits par le worker : SRT, qualité, clips…).
    Throttlé (au plus un pull par job par fenêtre) et best-effort : ne bloque jamais la
    requête — au pire la donnée apparaît au passage suivant.
    """
    cfg = get_config()
    if not artifact_store.is_pg_backend(cfg):
        return
    # Réservé aux requêtes authentifiées : pas de travail (SELECT par job_id arbitraire)
    # pour un anonyme — la route répondra 401/redirect de toute façon.
    if not (current_user and current_user.is_authenticated):
        return
    job_id = (request.view_args or {}).get("job_id")
    if job_id:
        artifact_store.pull_job_files_throttled(cfg, job_id)


@web_bp.after_app_request
def _push_job_files_after_write(response):
    """Backend `pg` : après une ÉCRITURE réussie portant un `job_id`, pousse en base les
    fichiers modifiés (contexte, participants, lexique, mapping locuteurs…).

    Hook global volontaire : tout endpoint d'écriture présent ou FUTUR est couvert sans
    enrôlement manuel (règle d'or du chantier — ne jamais supposer un disque commun).
    Idempotent et bon marché quand rien n'a changé (comparaison via manifeste local).
    Une erreur remonte (500) : une sauvegarde non durable ne doit pas paraître réussie.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400:
        cfg = get_config()
        job_id = (request.view_args or {}).get("job_id")
        if job_id and artifact_store.is_pg_backend(cfg):
            # WEB_WRITE_PREFIXES (pas `input/`) : ne pas annuler la purge terminale.
            artifact_store.push_job_files(cfg, job_id, prefixes=artifact_store.WEB_WRITE_PREFIXES)
    return response


DEFAULT_JOB_TITLE = "Réunion sans titre"
CONFIG_SECRET_SENTINEL = "********"
PROCESS_START_TIME = time.time()
LEXICON_DISPLAY_MAX_ENTRIES = 80
_RESOURCE_STATUS_CACHE_LOCK = threading.Lock()
_RESOURCE_STATUS_CACHE: dict[tuple, dict] = {}
_DEFAULT_RESOURCE_STATUS_CACHE_TTL_S = 5.0


def _audit_origin_from_url(value: str | None) -> str:
    parsed = urlparse(value or "")
    if not parsed.hostname:
        return ""
    if parsed.port:
        return f"{parsed.hostname}:{parsed.port}"
    return parsed.hostname


def _clean_job_title(title: str | None, default: str = DEFAULT_JOB_TITLE) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", "", title or "").strip()
    return (cleaned or default)[:255]


def _resource_status_cache_ttl_s(cfg: dict) -> float:
    raw = ((cfg.get("inference", {}) or {}).get("resilience", {}) or {}).get(
        "capabilities_cache_ttl_s",
        _DEFAULT_RESOURCE_STATUS_CACHE_TTL_S,
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_RESOURCE_STATUS_CACHE_TTL_S


def _resource_status_cache_key(cfg: dict, requirements: set[str]) -> tuple:
    inference = cfg.get("inference", {}) or {}
    models = cfg.get("models", {}) or {}
    stt = inference.get("stt", {}) or {}
    return (
        inference.get("mode", "local"),
        inference.get("url") or inference.get("base_url") or "",
        models.get("stt_backend", "cohere"),
        models.get("diarization_backend", "pyannote"),
        tuple(sorted(requirements)),
        repr(stt.get("backends", {})),
    )


def _get_cached_resource_status(key: tuple, now_s: float) -> dict | None:
    with _RESOURCE_STATUS_CACHE_LOCK:
        cached = _RESOURCE_STATUS_CACHE.get(key)
        if not cached:
            return None
        if float(cached["expires_at"]) <= now_s:
            _RESOURCE_STATUS_CACHE.pop(key, None)
            return None
        summary = copy.deepcopy(cached["summary"])
    summary["cached"] = True
    return summary


def _set_cached_resource_status(key: tuple, summary: dict, ttl_s: float, now_s: float) -> None:
    if ttl_s <= 0:
        return
    with _RESOURCE_STATUS_CACHE_LOCK:
        _RESOURCE_STATUS_CACHE[key] = {
            "expires_at": now_s + ttl_s,
            "summary": copy.deepcopy(summary),
        }


def _clear_resource_status_cache() -> None:
    """Réservé aux tests et aux changements explicites de config."""
    with _RESOURCE_STATUS_CACHE_LOCK:
        _RESOURCE_STATUS_CACHE.clear()


_FRISE_LEVEL_RANK = {"ok": 0, "suspect": 1, "degrade": 2}


def _fmt_mmss(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    return f"{total // 60}:{total % 60:02d}"


def _build_difficulty_frise(difficulty_map: list[dict] | None, max_buckets: int = 160) -> list[dict]:
    """Réduit la `difficulty_map` par fenêtre en une frise temporelle bornée pour l'UI.

    Regroupe les fenêtres consécutives en au plus `max_buckets` segments (le niveau
    d'un segment est le **pire** des fenêtres qu'il couvre), avec une largeur `pct`
    proportionnelle à la durée. Fonction pure, testable.

    Args:
        difficulty_map: liste `{start, end, difficulty, signals}` (cf. difficulty_map.py).
        max_buckets: nombre maximal de segments rendus (anti-DOM massif sur longue réunion).

    Returns:
        Liste `{start, end, level, pct, label, signals}` triée par début, ou [] si vide.
    """
    windows = [
        w for w in (difficulty_map or [])
        if w.get("start") is not None and w.get("end") is not None
    ]
    if not windows:
        return []
    windows.sort(key=lambda w: float(w["start"]))
    group_size = max(1, math.ceil(len(windows) / max_buckets))

    frise: list[dict] = []
    for i in range(0, len(windows), group_size):
        chunk = windows[i:i + group_size]
        start = float(chunk[0]["start"])
        end = float(chunk[-1]["end"])
        worst = "ok"
        signals: list[str] = []
        for w in chunk:
            level = str(w.get("difficulty") or "ok")
            if _FRISE_LEVEL_RANK.get(level, 0) > _FRISE_LEVEL_RANK.get(worst, 0):
                worst = level
            for sig in (w.get("signals") or []):
                if sig not in signals:
                    signals.append(sig)
        frise.append({
            "start": start,
            "end": end,
            "level": worst,
            "duration": max(end - start, 0.0),
            "label": f"{_fmt_mmss(start)}–{_fmt_mmss(end)}",
            "signals": signals,
        })

    # Largeur normalisée pour remplir exactement la barre (les fenêtres SQUIM se
    # chevauchent : la somme des durées ≠ span) — robuste au chevauchement et aux trous.
    total_duration = max(sum(seg["duration"] for seg in frise), 1e-9)
    for seg in frise:
        seg["pct"] = round(100 * seg["duration"] / total_duration, 3)
    return frise


def _audio_diagnostic_view(preflight: dict, audio_scene: dict | None = None) -> dict:
    if not preflight:
        return {}

    level = str(preflight.get("risk_level") or "ok")
    level_labels = {
        "ok": "Son exploitable",
        "suspect": "À surveiller",
        "degrade": "Son difficile",
    }
    level_classes = {
        "ok": "success",
        "suspect": "warning",
        "degrade": "danger",
    }
    flag_labels = {
        "audio_tres_faible": "volume très faible",
        "audio_faible": "volume faible",
        "snr_faible": "bruit de fond présent",
        "bande_etroite": "voix peu détaillée",
        "clipping_detecte": "saturation détectée",
        "risque_transcription_non_fiable": "vérification renforcée utile",
        "squim_stoi_faible": "intelligibilité réduite",
        "squim_pesq_faible": "qualité perceptive faible",
        "squim_sisdr_faible": "distorsion présente",
        "dnsmos_ovrl_faible": "qualité globale faible",
        "rt60_eleve": "réverbération marquée",
        "c50_faible": "clarté faible",
        "codec_artefact": "bande téléphonique (codec)",
        "overlap": "voix superposées",
        "sig_lt_bak": "parole peu nette",
    }
    flags = [str(flag) for flag in preflight.get("flags", []) if flag]
    reasons = [flag_labels.get(flag, flag.replace("_", " ")) for flag in flags]
    if "audio_tres_faible" in flags and "risque_transcription_non_fiable" in flags:
        message = "Le volume est très faible. La transcription sera probablement peu fiable — une relecture attentive est indispensable."
    else:
        message = {
            "ok": "Les caractéristiques audio ne montrent pas de risque majeur.",
            "suspect": "La transcription reste possible, mais certains passages pourront demander une vérification.",
            "degrade": "Le fichier est exploitable, avec un risque plus élevé sur certains mots ou passages.",
        }.get(level, "Diagnostic audio disponible.")

    squim = preflight.get("squim_global") or {}
    dnsmos = preflight.get("dnsmos_global") or {}
    summary = preflight.get("difficulty_summary") or {}
    advice = _audio_advice(dnsmos, flags) if level in {"suspect", "degrade"} else None

    frise = _build_difficulty_frise(preflight.get("difficulty_map"))
    for seg in frise:
        seg["reasons"] = [flag_labels.get(s, s.replace("_", " ")) for s in seg["signals"][:3]]

    return {
        "level": level,
        "label": level_labels.get(level, level),
        "class": level_classes.get(level, "secondary"),
        "message": message,
        "reasons": reasons[:6],
        "advice": advice,
        "recommended_mode": "quality" if level in {"suspect", "degrade"} else "fast",
        "perceptual": {
            "squim": {"stoi": squim.get("stoi"), "pesq": squim.get("pesq"), "sisdr": squim.get("sisdr")} if squim else None,
            "dnsmos": {"sig": dnsmos.get("sig"), "bak": dnsmos.get("bak"), "ovrl": dnsmos.get("ovrl")} if dnsmos else None,
        },
        "difficulty": {
            "windows": summary.get("windows"),
            "degrade": summary.get("degrade"),
            "suspect": summary.get("suspect"),
        } if summary.get("windows") else None,
        "frise": frise or None,
        "metrics": {
            "rms": preflight.get("rms"),
            "estimated_snr_db": preflight.get("estimated_snr_db"),
            "silence_ratio": preflight.get("silence_ratio"),
            "bandwidth_95_hz": preflight.get("bandwidth_95_hz"),
        },
        "scene": {
            "has_music": bool((audio_scene or {}).get("has_music", False)),
            "has_noise": bool((audio_scene or {}).get("has_noise", False)),
        },
    }


def _audio_advice(dnsmos: dict, flags: list[str]) -> dict | None:
    """Conseil actionnable depuis DNSMOS : distingue bruit dominant (BAK bas →
    débruitage utile) de parole intrinsèquement dégradée (SIG bas → WER difficile).
    Convention MOS : BAK bas = bruit de fond important ; SIG bas = parole altérée."""
    sig, bak = dnsmos.get("sig"), dnsmos.get("bak")
    if sig is not None and bak is not None:
        if bak < sig:
            return {
                "class": "info",
                "text": f"Bruit de fond dominant — un débruitage peut aider (BAK {bak} < SIG {sig}).",
            }
        if sig < bak:
            return {
                "class": "warning",
                "text": f"Parole elle-même dégradée — vérification renforcée conseillée (SIG {sig} < BAK {bak}).",
            }
    if "codec_artefact" in flags:
        return {"class": "info", "text": "Bande passante de type téléphonique détectée (codec)."}
    return None


def _recover_summary_speaker_hints(fs: JobFilesystem, meeting: dict) -> dict:
    """Récupère les champs participants LLM si un ancien parsing les a manqués."""
    if meeting.get("speaker_roles_llm") or meeting.get("participants_detectes"):
        return meeting

    summary_text = meeting.get("summary_llm") or fs.load_text("summary/summary.md") or ""
    if not summary_text.strip():
        return meeting

    from transcria.gpu.opencode_runner import OpenCodeRunner

    parsed = OpenCodeRunner._parse_structured_summary(summary_text, language=meeting.get("language", "fr"))
    speaker_roles = parsed.get("speaker_roles") or {}
    participants_detectes = parsed.get("participants_detectes") or ""
    if not speaker_roles and not participants_detectes:
        return meeting

    recovered = dict(meeting)
    if participants_detectes:
        recovered["participants_detectes"] = participants_detectes
    if speaker_roles:
        recovered["speaker_roles_llm"] = speaker_roles
    fs.save_json("context/meeting_context.json", recovered)
    logger.info(
        "Champs participants LLM récupérés depuis summary.md | speaker_roles=%d",
        len(speaker_roles),
    )
    return recovered


def _selected_lexicon_ids(fs: JobFilesystem, central_lexicons: list) -> set[str]:
    stored = fs.load_json("context/selected_lexicons.json") or {}
    available_ids = {lexicon.id for lexicon in central_lexicons}
    if not isinstance(stored, dict) or "selected_lexicon_ids" not in stored:
        return set(available_ids)
    selected = {str(item) for item in stored.get("selected_lexicon_ids") or []}
    return selected.intersection(available_ids)


def _central_lexicon_reference_text(fs: JobFilesystem, meeting: dict) -> str:
    chunks = [
        fs.load_text("summary/quick_transcript.txt") or "",
        fs.load_text("summary/summary.md") or "",
    ]
    if isinstance(meeting, dict):
        for key in ("title", "title_suggere", "subject", "sujet_suggere", "objective", "objectif_suggere", "notes_suggeres"):
            value = meeting.get(key)
            if value:
                chunks.append(str(value))
    return "\n".join(chunks)


def _central_lexicon_cards(central_lexicons: list, selected_ids: set[str]) -> list[dict]:
    return [
        {
            "id": lexicon.id,
            "name": lexicon.name,
            "description": lexicon.description,
            "group_id": lexicon.group_id,
            "group_name": lexicon.group.name if lexicon.group else "",
            "entry_count": len(lexicon.entries or []),
            "selected": lexicon.id in selected_ids,
        }
        for lexicon in central_lexicons
    ]


def _central_lexicon_context(
    job: Job,
    fs: JobFilesystem,
    session_lexicon: list[dict],
    meeting: dict,
) -> tuple[list[dict], list[dict], dict]:
    """Retourne les lexiques centraux du job et le lexique initial à afficher."""
    central_lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
    selected_ids = _selected_lexicon_ids(fs, central_lexicons)
    selected_lexicons = [lexicon for lexicon in central_lexicons if lexicon.id in selected_ids]
    central_entries = CentralLexiconStore.entries_for_lexicons(selected_lexicons)
    llm_suggestions = (meeting.get("termes_suspects") or []) if isinstance(meeting, dict) else []
    metadata = {
        "available_lexicons": len(central_lexicons),
        "selected_lexicons": len(selected_lexicons),
        "selected_entries": len(central_entries),
        "llm_suggestions": len(llm_suggestions),
        "displayed_central_entries": 0,
        "hidden_central_entries": 0,
        "limited_out": 0,
        "session_existing": bool(session_lexicon),
        "max_entries": LEXICON_DISPLAY_MAX_ENTRIES,
    }
    cards = _central_lexicon_cards(central_lexicons, selected_ids)
    if session_lexicon:
        logger.info(
            "Lexique étape 6: session existante conservée | job=%s, session_entries=%d, central_lexicons=%d, selected_lexicons=%d",
            job.id,
            len(session_lexicon),
            len(central_lexicons),
            len(selected_lexicons),
        )
        return cards, session_lexicon, metadata

    display_entries, display_stats = prefilter_lexicon_entries_for_display(
        central_entries,
        _central_lexicon_reference_text(fs, meeting),
        max_entries=LEXICON_DISPLAY_MAX_ENTRIES,
    )
    metadata.update({
        "displayed_central_entries": display_stats.get("kept", 0),
        "hidden_central_entries": display_stats.get("hidden", 0),
        "limited_out": display_stats.get("limited_out", 0),
        "kept_by_term_presence": display_stats.get("kept_by_term_presence", 0),
        "kept_by_variant_presence": display_stats.get("kept_by_variant_presence", 0),
        "kept_by_priority": display_stats.get("kept_by_priority", 0),
        "reference_available": display_stats.get("reference_available", False),
    })
    merged = merge_lexicon_entries(display_entries, llm_suggestions)
    if central_entries or llm_suggestions:
        logger.info(
            "Lexique étape 6: préremplissage fusionné | job=%s, central_lexicons=%d, selected_lexicons=%d,"
            " central_entries=%d, displayed_central=%d, hidden_central=%d, llm_suggestions=%d, merged=%d",
            job.id,
            len(central_lexicons),
            len(selected_lexicons),
            len(central_entries),
            metadata["displayed_central_entries"],
            metadata["hidden_central_entries"],
            len(llm_suggestions),
            len(merged),
        )
    return cards, merged, metadata


def _processing_diagnostic_view(metadata: dict, segments: list) -> dict:
    reliability_counts: dict[str, int] = {}
    suspect_segments: list[dict] = []
    if isinstance(segments, list):
        for index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            level = str(segment.get("reliability") or "")
            if level:
                reliability_counts[level] = reliability_counts.get(level, 0) + 1
            if level in {"suspect", "degrade"} and len(suspect_segments) < 8:
                suspect_segments.append({
                    "index": index,
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "speaker": segment.get("speaker", ""),
                    "level": level,
                    "reasons": segment.get("reliability_reasons") or [],
                    "text": str(segment.get("text", "") or "").strip()[:180],
                })

    return {
        "backend": metadata.get("backend", ""),
        "chunking_mode": metadata.get("chunking_mode", ""),
        "segments": metadata.get("segments") or len(segments or []),
        "speaker_count": metadata.get("speaker_count"),
        "vad_final_enabled": bool(metadata.get("vad_final_enabled", False)),
        "reliability_counts": reliability_counts,
        "suspect_segments": suspect_segments,
    }


def _enrich_lexicon_context_audio(lexicon: list[dict], segments: list | None = None) -> list[dict]:
    """Ajoute des bornes audio parsées aux contextes de lexique pour l'UI."""
    enriched = copy.deepcopy(lexicon)
    summary_segments = segments if isinstance(segments, list) else []
    total_playable = 0
    total_contexts = 0
    for term in enriched:
        contexts = term.get("contexts")
        if not isinstance(contexts, list):
            continue
        listened = 0
        playable = 0
        for context in contexts:
            if not isinstance(context, dict):
                continue
            _repair_lexicon_context_timecode(context)
            parsed = parse_time_range(str(context.get("timecode", "")))
            if parsed is not None:
                context["audio_start"] = round(parsed[0], 3)
                context["audio_end"] = round(parsed[1], 3)
                context["audio_available"] = True
                playable += 1
            elif context.get("quote"):
                estimated = _resolve_context_audio_range("", str(context.get("quote", "")), summary_segments)
                if estimated is not None:
                    context["audio_start"] = round(estimated[0], 3)
                    context["audio_end"] = round(estimated[1], 3)
                    context["audio_available"] = True
                    context["audio_estimated_from_quote"] = True
                    playable += 1
                else:
                    context["audio_available"] = False
            else:
                context["audio_available"] = False
            if bool(context.get("listened", False)):
                listened += 1
        term["contexts_listened_count"] = listened
        term["contexts_playable_count"] = playable
        total_playable += playable
        total_contexts += len(contexts)
    if total_contexts:
        logger.debug(
            "Lexique enrichi pour l'UI: %d terme(s) avec contexte(s), %d/%d jouables",
            sum(1 for t in enriched if t.get("contexts")),
            total_playable,
            total_contexts,
        )
    return enriched


def _strip_lexicon_context_wrappers(value: str) -> str:
    text = str(value or "").strip()
    pairs = {
        '"': '"',
        "'": "'",
        "«": "»",
        "“": "”",
        "‘": "’",
        "`": "`",
    }
    while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def _clean_lexicon_context_timecode(value: str) -> str:
    text = _strip_lexicon_context_wrappers(value)
    text = text.strip().strip("[]").strip()
    return _strip_lexicon_context_wrappers(text)


def _clean_lexicon_context_quote(value: str) -> str:
    text = _strip_lexicon_context_wrappers(value)
    text = text.strip().strip("|").strip()
    return _strip_lexicon_context_wrappers(text)


def _repair_lexicon_context_timecode(context: dict) -> None:
    """Répare les contextes LLM dont le timecode ou la citation contiennent des guillemets parasites."""
    raw_timecode = str(context.get("timecode", "") or "")
    cleaned_timecode = _clean_lexicon_context_timecode(raw_timecode)
    if cleaned_timecode != raw_timecode.strip():
        context["timecode"] = cleaned_timecode

    quote = _clean_lexicon_context_quote(str(context.get("quote", "") or ""))
    if quote and quote != context.get("quote"):
        context["quote"] = quote

    if parse_time_range(cleaned_timecode) is not None:
        return
    if not quote:
        return

    timestamp = r"(?:\d+(?:[\.,]\d+)?s|\d{1,3}:\d{2}(?::\d{2})?(?:[\.,]\d+)?s?)"
    time_range = rf"{timestamp}(?:\s*(?:→|->|-)\s*{timestamp})?"
    match = re.match(
        rf'^[«"“]?\[?(?P<timecode>{time_range})\]?[»"”]?\s*'
        rf'(?:(?P<speaker>SPEAKER_[A-Za-z0-9]+)\s*:\s*)?'
        rf'(?P<quote>.+?)\s*$',
        quote,
    )
    if not match:
        return
    context["timecode"] = _clean_lexicon_context_timecode(match.group("timecode"))
    parsed_quote = _clean_lexicon_context_quote(match.group("quote") or "")
    if parsed_quote:
        context["quote"] = parsed_quote
    if match.group("speaker") and not context.get("speaker"):
        context["speaker"] = match.group("speaker").strip()


def _normalize_context_quote(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _find_quote_span(quote: str, text: str) -> tuple[int, int] | None:
    """Retrouve approximativement la position d'une citation dans un texte."""
    normalized_quote = _normalize_context_quote(quote)
    normalized_text = _normalize_context_quote(text)
    if not normalized_quote or not normalized_text:
        return None
    index = normalized_text.find(normalized_quote)
    if index < 0:
        return None
    return index, index + len(normalized_quote)


def _find_segment_for_quote(quote: str, segments: list) -> dict | None:
    normalized_quote = _normalize_context_quote(quote)
    if not normalized_quote:
        return None
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        normalized_text = _normalize_context_quote(str(segment.get("text", "")))
        if normalized_quote and normalized_quote in normalized_text:
            return segment
    return None


def _estimate_quote_range_in_segment(quote: str, segment: dict) -> tuple[float, float] | None:
    """Estime le timecode d'une citation à l'intérieur d'un segment STT long."""
    try:
        seg_start = float(segment.get("start"))  # type: ignore[arg-type]
        seg_end = float(segment.get("end"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seg_end <= seg_start:
        return None

    span = _find_quote_span(quote, str(segment.get("text", "")))
    if span is None:
        return None

    normalized_text = _normalize_context_quote(str(segment.get("text", "")))
    text_len = max(1, len(normalized_text))
    duration = seg_end - seg_start
    quote_start = seg_start + duration * (span[0] / text_len)
    quote_end = seg_start + duration * (span[1] / text_len)
    return quote_start, max(quote_start + 0.5, quote_end)


def _resolve_context_audio_range(timecode: str, quote: str, segments: list) -> tuple[float, float, bool] | None:
    parsed = parse_time_range(timecode)
    matched_segment = _find_segment_for_quote(quote, segments)
    if not matched_segment:
        return (*parsed, False) if parsed is not None else None

    estimated = _estimate_quote_range_in_segment(quote, matched_segment)
    if estimated is None:
        return (*parsed, False) if parsed is not None else None
    quote_start, quote_end = estimated

    if parsed is None:
        return quote_start, quote_end, True

    start, end = parsed
    if abs(start - quote_start) > 2.0 or abs(end - quote_end) > 2.0:
        return quote_start, quote_end, True
    return start, end, False


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


def _fill_missing_speaker_genders(
    speakers_data: dict,
    mapping_data: dict,
    audio_scene: dict,
    speaker_turns: dict,
) -> bool:
    """Complète les genres manquants depuis mapping puis analyse acoustique."""
    speakers = speakers_data.get("speakers") if isinstance(speakers_data, dict) else None
    if not isinstance(speakers, list):
        return False

    changed = False
    mapping = mapping_data.get("mapping", {}) if isinstance(mapping_data, dict) else {}
    mapped_speakers = {
        item.get("speaker_id"): item
        for item in mapping_data.get("speakers", [])
        if isinstance(item, dict) and item.get("speaker_id")
    } if isinstance(mapping_data, dict) else {}

    for speaker in speakers:
        if not isinstance(speaker, dict) or speaker.get("gender"):
            continue
        speaker_id = speaker.get("speaker_id")
        mapped_gender = ""
        if isinstance(mapping.get(speaker_id), dict):
            mapped_gender = mapping[speaker_id].get("gender", "")
        if not mapped_gender and isinstance(mapped_speakers.get(speaker_id), dict):
            mapped_gender = mapped_speakers[speaker_id].get("gender", "")
        if mapped_gender in {"female", "male"}:
            speaker["gender"] = mapped_gender
            changed = True

    if all((not isinstance(s, dict)) or s.get("gender") for s in speakers):
        return changed

    gender_segments = (audio_scene or {}).get("gender_segments") or []
    turns = (speaker_turns or {}).get("exclusive_turns") or (speaker_turns or {}).get("turns") or []
    if not gender_segments or not turns:
        return changed

    try:
        from transcria.workflow.runner import WorkflowRunner

        speaker_genders = WorkflowRunner._assign_speaker_genders(gender_segments, turns)
    except Exception:
        return changed

    for speaker in speakers:
        if not isinstance(speaker, dict) or speaker.get("gender"):
            continue
        gender = (speaker_genders.get(speaker.get("speaker_id")) or {}).get("gender")
        if gender in {"female", "male"}:
            speaker["gender"] = gender
            changed = True
    return changed


def _can_access_job(job, user) -> bool:
    return (
        job is not None
        and (
            job.owner_id == user.id
            or user.has_role(Role.ADMIN)
            or GroupStore.users_share_group(user.id, job.owner_id)
        )
    )


def _require_job_access(job, user):
    if job is None:
        abort(404)
    if not _can_access_job(job, user):
        logger.warning(
            "Accès refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            getattr(user, "id", None),
            getattr(user, "role", None),
            job.owner_id,
        )
        abort(403)


def _get_job_for_api(job_id: str):
    job = JobStore.get_by_id(job_id)
    if job is None:
        return None, (jsonify({"error": "Job not found"}), 404)
    if not _can_access_job(job, current_user):
        logger.warning(
            "Accès API refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            current_user.id,
            getattr(current_user, "role", None),
            job.owner_id,
        )
        return None, (jsonify({"error": "Accès interdit"}), 403)
    return job, None


def _can_manage_queue_job(job) -> bool:
    if job is None or not current_user.is_authenticated:
        return False
    if current_user.has_role(Role.ADMIN):
        return True
    if not GroupStore.is_group_admin(current_user):
        return False
    return GroupStore.users_share_group(current_user.id, job.owner_id)


def _config_for_display(cfg: dict) -> dict:
    display_cfg = copy.deepcopy(cfg)
    auth_cfg = display_cfg.get("auth")
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password"):
        auth_cfg["first_admin_password"] = CONFIG_SECRET_SENTINEL
    return display_cfg


def _restore_masked_config_secrets(submitted: dict, current_cfg: dict) -> dict:
    restored = copy.deepcopy(submitted)
    auth_cfg = restored.get("auth")
    current_auth = current_cfg.get("auth", {})
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password") == CONFIG_SECRET_SENTINEL:
        auth_cfg["first_admin_password"] = current_auth.get("first_admin_password", "")
    return restored


def _extract_synthese(md_text: str) -> str:
    """Extrait uniquement la section Synthèse du markdown LLM."""
    import re
    m = re.search(r'## Synthèse\s*\n(.+?)(?:\n##|\Z)', md_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: prend les dernières lignes après le dernier ##
    parts = md_text.split('## ')
    if len(parts) > 1:
        last = parts[-1]
        lines = last.split('\n', 1)
        if len(lines) > 1:
            return lines[1].strip()
    return md_text[:800]


def _check_database_health() -> tuple[bool, str | None]:
    try:
        db.session.execute(db.select(1)).scalar()
        return True, None
    except Exception as exc:
        logger.exception("Healthcheck base de données en échec")
        return False, str(exc)


def _collect_job_state_counts() -> dict[str, int]:
    rows = db.session.execute(
        db.select(Job.state, func.count(Job.id)).group_by(Job.state)
    ).all()
    return {state: count for state, count in rows}


def _render_prometheus_metrics() -> str:
    db_ok, _ = _check_database_health()
    state_counts = _collect_job_state_counts() if db_ok else {}
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else {
        "queued_jobs": 0,
        "running_jobs": 0,
        "max_workers": 0,
    }
    try:
        from transcria.queue.store import QueueStore

        queue_counts = QueueStore.count_by_status() if db_ok else {}
    except Exception:
        queue_counts = {}
    try:
        blob_stats = artifact_store.store_stats() if db_ok else {"files": 0, "bytes": 0}
    except Exception:
        blob_stats = {"files": 0, "bytes": 0}
    lines = [
        "# HELP transcria_up Indique si le service TranscrIA est disponible.",
        "# TYPE transcria_up gauge",
        f"transcria_up {1 if db_ok else 0}",
        "# HELP transcria_ready Indique si le service accepte de nouveaux jobs.",
        "# TYPE transcria_ready gauge",
        f"transcria_ready {1 if db_ok and executor is not None else 0}",
        "# HELP transcria_process_start_time_seconds Horodatage Unix du démarrage du process web.",
        "# TYPE transcria_process_start_time_seconds gauge",
        f"transcria_process_start_time_seconds {PROCESS_START_TIME:.0f}",
        "# HELP transcria_jobs_total Nombre total de jobs en base.",
        "# TYPE transcria_jobs_total gauge",
        f"transcria_jobs_total {sum(state_counts.values())}",
        "# HELP transcria_worker_jobs Nombre de jobs suivis par le worker interne.",
        "# TYPE transcria_worker_jobs gauge",
        f'transcria_worker_jobs{{status="queued"}} {runtime["queued_jobs"]}',
        f'transcria_worker_jobs{{status="running"}} {runtime["running_jobs"]}',
        "# HELP transcria_worker_capacity Nombre maximal de jobs simultanés pour le worker interne.",
        "# TYPE transcria_worker_capacity gauge",
        f"transcria_worker_capacity {runtime['max_workers']}",
        "# HELP transcria_queue_entries Nombre d'entrées dans la file persistante.",
        "# TYPE transcria_queue_entries gauge",
        f'transcria_queue_entries{{status="waiting"}} {queue_counts.get("waiting", 0)}',
        f'transcria_queue_entries{{status="paused"}} {queue_counts.get("paused", 0)}',
        f'transcria_queue_entries{{status="running"}} {queue_counts.get("running", 0)}',
        "# HELP transcria_job_files_total Fichiers de jobs répliqués en base (storage.shared_backend=pg ; 0 en fs).",
        "# TYPE transcria_job_files_total gauge",
        f"transcria_job_files_total {blob_stats['files']}",
        "# HELP transcria_job_files_bytes Volume (octets) des fichiers de jobs répliqués en base "
        "(croissance continue = purge input/ qui ne joue pas).",
        "# TYPE transcria_job_files_bytes gauge",
        f"transcria_job_files_bytes {blob_stats['bytes']}",
        "# HELP transcria_jobs_state Nombre de jobs par état.",
        "# TYPE transcria_jobs_state gauge",
    ]
    for state in sorted(state_counts):
        lines.append(f'transcria_jobs_state{{state="{state}"}} {state_counts[state]}')
    return "\n".join(lines) + "\n"


@web_bp.route("/")
@login_required
def index():
    cfg = get_config()
    retention_days = cfg.get("security", {}).get("retention_days")
    purged = JobStore.purge_expired_jobs(retention_days, cfg["storage"]["jobs_dir"])
    if purged:
        logger.info("Purge rétention: %d jobs supprimés", purged)
    audit_retention = cfg.get("security", {}).get("audit_retention_days", 1095)
    if isinstance(audit_retention, (int, float)) and audit_retention > 0:
        from transcria.audit.store import AuditStore
        AuditStore.purge_expired_by_policy(
            int(audit_retention),
            cfg.get("security", {}).get("audit_retention_by_family") or {},
        )
    jobs = JobStore.list_for_user(current_user, include_all=current_user.has_role(Role.ADMIN))
    return render_template("index.html", jobs=jobs, roles=Role)


@web_bp.route("/jobs/new", methods=["POST"])
@login_required
@requires(Permission.CREATE_JOBS)
def create_job():
    title = _clean_job_title(request.form.get("title"))
    job = JobStore.create_job(owner_id=current_user.id, title=title)
    flash(_("Nouveau traitement créé."), "success")
    return redirect(url_for("web.job_wizard", job_id=job.id))


@web_bp.route("/jobs/<job_id>")
@login_required
def job_wizard(job_id: str):
    from transcria.workflow.profile_availability import compute_profiles_view, compute_wizard_layout
    from transcria.workflow.profiles import get_profile, profile_for_job

    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)
    assert job is not None

    statuses = WorkflowState.compute_statuses(
        job.state,
        job.get_extra_data().get("last_non_terminal_state"),
    )
    steps = WorkflowState.get_steps()
    next_step = WorkflowState.get_next_step(statuses)

    # Le profil pilote la disposition du wizard (étapes affichées/masquées, numérotation).
    # Profil sélectionné = celui persisté sur le job, sinon le recommandé (présélection du
    # curseur) ; None ⇒ comportement complet (legacy/aucun profil dispo).
    profiles_view = compute_profiles_view(cfg)
    selected_profile = profile_for_job(job)

    # Types de réunion : intégrés + personnalisés VISIBLES DU PROPRIÉTAIRE du job
    # (même règle que les lexiques : un admin qui consulte voit le catalogue du
    # propriétaire, pas le sien). Champs spécifiques fusionnés pour le JS étape 4.
    from transcria.auth.store import UserStore as _UserStore
    from transcria.context.meeting_type_store import MeetingTypeStore
    _owner = _UserStore.get_by_id(job.owner_id)
    if _owner is not None:
        builtin_meeting_types, custom_meeting_types, merged_type_fields = (
            MeetingTypeStore.merged_catalog_for_user(_owner)
        )
    else:  # propriétaire supprimé : catalogue intégré seul (le wizard reste servable)
        from transcria.context.meeting_type_catalog import meeting_type_names, type_specific_fields
        builtin_meeting_types = meeting_type_names()
        custom_meeting_types = []
        merged_type_fields = type_specific_fields()
    # Affichage traduit des types intégrés dans la locale de l'INTERFACE (axe A). Le
    # `name` reste la CLÉ (value de l'<option>, posté en meeting_type, lookups/comparaisons) ;
    # seule l'étiquette visible est localisée. Custom = déjà dans la langue de l'auteur.
    from transcria.context.meeting_type_catalog import localized_type_display
    from transcria.web.i18n import select_locale
    _ui_locale = select_locale()
    meeting_type_display = {
        mt: localized_type_display(mt, _ui_locale, "name", mt) for mt in builtin_meeting_types
    }
    if selected_profile is None and profiles_view.get("recommended"):
        selected_profile = get_profile(profiles_view["recommended"])
    wizard_layout = compute_wizard_layout(selected_profile, statuses)
    selected_profile_id = selected_profile.id if selected_profile else ""

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    summary_data = fs.load_json("summary/summary.json") or {}
    meeting = MeetingContextManager.get(job, cfg["storage"]["jobs_dir"])
    meeting = _recover_summary_speaker_hints(fs, meeting)
    session_lexicon = LexiconManager.get(job, cfg["storage"]["jobs_dir"])
    central_lexicons, initial_lexicon, central_lexicon_display = _central_lexicon_context(job, fs, session_lexicon, meeting)
    summary_segments = summary_data.get("segments") if isinstance(summary_data, dict) else []
    lexicon = _enrich_lexicon_context_audio(initial_lexicon, summary_segments)
    speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
    voice_matches = fs.load_json("speakers/voice_matches.json") or {}
    audio_scene = fs.load_json("metadata/audio_scene.json") or {}
    speaker_turns = fs.load_json("speakers/speaker_turns.json") or {}
    # Fusionner mapping + participants pour pré-remplir nom/fonction/rôle
    mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
    mapped_speakers = mapping_data.get("speakers", [])
    participants = ParticipantsManager.get(job, cfg["storage"]["jobs_dir"])
    speaker_role_hints = meeting.get("speaker_roles_llm", {}) if isinstance(meeting, dict) else {}
    if mapped_speakers:
        for s in speakers_data.get("speakers", []):
            for ms in mapped_speakers:
                if ms.get("speaker_id") == s.get("speaker_id"):
                    s["mapped_name"] = ms.get("mapped_name")
                    s["mapped_to"] = ms.get("mapped_to")
            # Enrichir avec nom/fonction/rôle depuis participants
            for p in participants:
                if p.get("id") == s.get("mapped_to") or p.get("name") == s.get("mapped_name"):
                    s["mapped_func"] = p.get("function", "")
                    s["mapped_role"] = p.get("role", "")
                    pname = p.get("name", "")
                    if pname and not pname.upper().startswith("SPEAKER_"):
                        s["mapped_name"] = pname
    elif speaker_role_hints:
        from transcria.workflow.runner import WorkflowRunner

        for s in speakers_data.get("speakers", []):
            speaker_id = s.get("speaker_id")
            hint = speaker_role_hints.get(speaker_id)
            if not hint:
                continue
            normalized = WorkflowRunner._normalize_speaker_role_info(hint)
            if normalized["label"]:
                s["mapped_name"] = normalized["label"]
            if normalized["role"]:
                s["mapped_role"] = normalized["role"]
    if _fill_missing_speaker_genders(speakers_data, mapping_data, audio_scene, speaker_turns):
        fs.save_json("speakers/speaker_stats.json", speakers_data)
    audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
    audio_preflight = fs.load_json("metadata/audio_preflight.json") or {}
    transcription_metadata = fs.load_json("metadata/transcription_metadata.json") or {}
    transcription_segments = fs.load_json("metadata/transcription_segments.json") or []
    quality_report = fs.load_json("quality/quality_report.json") or {}
    srt_content = _effective_srt(fs) or ""

    # Estimation de temps CALIBRÉE MACHINE, spécifique au profil choisi (remplace la
    # formule fixe historique). Fourchette + base de confiance pour un affichage honnête.
    timing_estimate = None
    try:
        from transcria.workflow.profiles import get_profile, is_profile
        from transcria.workflow.timing_service import estimate_total_with_human

        _audio_s = float(audio_analysis.get("duration_seconds") or 0)
        _prof = get_profile(selected_profile_id) if selected_profile_id and is_profile(selected_profile_id) else None
        if _prof is not None and _audio_s > 0:
            timing_estimate = estimate_total_with_human(_prof, _audio_s)
    except Exception:  # noqa: BLE001 — l'estimation ne doit jamais casser le wizard
        timing_estimate = None

    return render_template(
        "job_wizard.html",
        timing_estimate=timing_estimate,
        job=job,
        steps=steps,
        statuses=statuses,
        next_step=next_step,
        summary=summary_data,
        meeting=meeting,
        participants=participants,
        lexicon=lexicon,
        central_lexicons=central_lexicons,
        central_lexicon_display=central_lexicon_display,
        central_lexicon_prefill_count=central_lexicon_display.get("displayed_central_entries", 0),
        session_lexicon_exists=bool(session_lexicon),
        speakers=speakers_data,
        voice_matches=voice_matches,
        audio_analysis=audio_analysis,
        audio_preflight=audio_preflight,
        audio_diagnostic=_audio_diagnostic_view(audio_preflight, audio_scene),
        processing_profiles=profiles_view,
        wizard_layout=wizard_layout,
        selected_profile_id=selected_profile_id,
        audio_scene=audio_scene,
        processing_diagnostic=_processing_diagnostic_view(transcription_metadata, transcription_segments),
        quality_report=quality_report,
        srt_content=srt_content,
        meeting_types=builtin_meeting_types,
        meeting_type_display=meeting_type_display,
        custom_meeting_types=custom_meeting_types,
        type_specific_fields_json=json.dumps(merged_type_fields, ensure_ascii=False),
        lexicon_categories=LEXICON_CATEGORIES,
        lexicon_priorities=LEXICON_PRIORITIES,
        promote_lexicons=_promote_lexicons_view(),
        promote_groups=_promote_groups_view(),
        promote_allowed=_promote_allowed(),
        voice_enrollment_enabled=bool(cfg.get("voice_enrollment", {}).get("enabled", False)),
        llm_timeout=int(
            cfg.get("workflow", {}).get("arbitration_llm", {}).get("timeout_seconds", 7200)
        ),
    )


@web_bp.route("/jobs/<job_id>/result")
@login_required
def job_result(job_id: str):
    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)
    assert job is not None

    # R2 (revue macro) : la page « Résultat » affiche « Terminé » en dur — ne la rendre
    # que pour un job réellement COMPLETED. Un job échoué/en cours/en attente est renvoyé
    # vers sa page de traitement (état réel, pas un faux « Terminé »).
    if job.state != JobState.COMPLETED:
        return redirect(url_for("web.job_wizard", job_id=job.id))

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    quality_report = fs.load_json("quality/quality_report.json") or {}
    review_points = fs.load_json("quality/review_points.json") or []
    srt_content = _effective_srt(fs) or ""
    # R1 (revue macro) : gater les boutons sur la CAPACITÉ du profil, pas l'existence du
    # fichier — le DOCX est (re)généré à la volée au téléchargement, un profil SRT-only
    # (docx_level/zip_level == "none") ne doit simplement pas montrer ces boutons.
    from transcria.workflow.profiles import profile_for_job

    profile = profile_for_job(job)
    has_docx = profile is None or profile.docx_level != "none"
    has_package = profile is None or profile.zip_level != "none"

    return render_template(
        "job_result.html",
        job=job,
        quality_report=quality_report,
        review_points=review_points,
        srt_content=srt_content,
        has_package=has_package,
        has_docx=has_docx,
    )


# --- API endpoints ---

@web_bp.route("/health")
def health():
    db_ok, db_error = _check_database_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "transcria",
        "database": {
            "status": "ok" if db_ok else "error",
        },
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if db_ok else 503)


@web_bp.route("/ready")
def ready():
    db_ok, db_error = _check_database_health()
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else None
    ready_ok = db_ok and executor is not None
    payload = {
        "status": "ready" if ready_ok else "not_ready",
        "service": "transcria",
        "database": {"status": "ok" if db_ok else "error"},
        "worker": runtime or {"healthy": False},
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if ready_ok else 503)


@web_bp.route("/metrics")
def metrics():
    return Response(_render_prometheus_metrics(), mimetype="text/plain; version=0.0.4; charset=utf-8")

@web_bp.route("/api/jobs/<job_id>/upload", methods=["POST"])
@login_required
def api_upload(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
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
        job.title = _clean_job_title(Path(file.filename).stem or file.filename)
    return jsonify(info)


@web_bp.route("/api/jobs/<job_id>/analyze", methods=["POST"])
@login_required
def api_analyze(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    result = JobService.analyze(job.id, cfg["storage"]["jobs_dir"], cfg)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/summary", methods=["POST"])
@login_required
def api_summary(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
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
    from transcria.queue.store import QUEUE_PAUSED, QUEUE_RUNNING, QUEUE_WAITING, QueueStore
    from transcria.services.job_executor import SUMMARY_MODE

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

    from transcria.workflow.runner import WorkflowRunner

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
            from transcria.notifications.admin_alerts import alert_admin_vram_wait

            alert_admin_vram_wait(cfg, job, required_mb=required_mb, phase=phase)
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
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    body, err = _json_body(dict)
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
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    body, err = _json_body(dict)
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
    from transcria.workflow import resume

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
    job, error_response = _get_job_for_api(job_id)
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
    job, error_response = _get_job_for_api(job_id)
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
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    data, _json_err = _json_body(dict)
    if _json_err:
        return _json_err
    # Type de réunion : validé contre le catalogue visible du PROPRIÉTAIRE (intégrés +
    # personnalisés). Un type personnalisé est MATÉRIALISÉ dans le job (sa fiche complète,
    # sans binaire) : le rendu et le worker n'ont jamais à résoudre un template en base —
    # robuste en topologie split, et la suppression du template ne casse aucun job.
    if data.get("meeting_type"):
        from transcria.context.meeting_type_catalog import meeting_type_names
        from transcria.context.meeting_type_store import MeetingTypeStore

        chosen = str(data["meeting_type"])
        if chosen in meeting_type_names():
            data["custom_type"] = None
            stale_logo = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).job_dir / "context" / "type_logo.png"
            if stale_logo.exists():
                stale_logo.unlink()
        else:
            from transcria.auth.store import UserStore as _UserStore
            _owner = _UserStore.get_by_id(job.owner_id)
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
    MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], data)
    audit_log(AuditAction.JOB_CONTEXT_SAVE, target_type="job", target_id=job.id, target_label=job.title)
    if job.state == JobState.SUMMARY_DONE.value:
        JobStore.update_state(job.id, JobState.CONTEXT_DONE)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/participants", methods=["POST"])
@login_required
def api_participants(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    data, _json_err = _json_body(list)
    if _json_err:
        return _json_err
    ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], data)
    audit_log(AuditAction.JOB_PARTICIPANTS_SAVE, target_type="job", target_id=job.id, target_label=job.title)
    if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
        JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
    return jsonify({"status": "ok"})


def _json_body(expected: type):
    """Corps JSON d'une API, TYPÉ et tolérant (banc fuzz C0.2).

    - corps absent / null / JSON invalide → valeur vide du type attendu (comportement
      historique de ``request.get_json() or {}``), jamais de page HTML 400 ;
    - corps du MAUVAIS type racine (ex. une chaîne) → (None, 400 JSON propre) au lieu
      d'un AttributeError 500 sur ``data.get``.
    """
    data = request.get_json(silent=True)
    if data is None:
        return (expected(), None)
    if not isinstance(data, expected):
        attendu = "objet" if expected is dict else "liste"
        return (None, (jsonify({"error": f"Corps JSON invalide : {attendu} attendu."}), 400))
    return (data, None)


def _promote_allowed() -> bool:
    from transcria.context.central_lexicon_store import CentralLexiconStore
    return CentralLexiconStore.can_manage_lexicons(current_user)


def _promote_lexicons_view() -> list[dict]:
    """Lexiques centraux que l'utilisateur courant peut ALIMENTER depuis l'étape 6."""
    from transcria.context.central_lexicon_store import CentralLexiconStore
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return []
    return [{"id": lx.id, "name": lx.name, "group_name": lx.group.name if lx.group else "global"}
            for lx in CentralLexiconStore.list_manageable_lexicons(current_user)]


def _promote_groups_view() -> list[dict]:
    from transcria.auth.groups import GroupStore
    from transcria.context.central_lexicon_store import CentralLexiconStore
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return []
    return [{"id": g.id, "name": g.name} for g in GroupStore.list_for_admin(current_user)]


@web_bp.route("/api/jobs/<job_id>/lexicon/promote", methods=["POST"])
@login_required
def api_lexicon_promote(job_id: str):
    """Étape 6 : pousser une forme validée du lexique de SESSION vers un lexique
    CENTRAL (existant ou créé à la volée) — même périmètre de droits que la gestion
    des lexiques (admin de groupe / admin)."""
    from transcria.context.central_lexicon_store import (
        CentralLexiconAccessError,
        CentralLexiconStore,
        CentralLexiconValidationError,
    )

    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return jsonify({"error": "Réservé aux administrateurs de lexiques."}), 403

    data, _json_err = _json_body(dict)
    if _json_err:
        return _json_err
    term = str(data.get("term") or "").strip()
    if not term:
        return jsonify({"error": "La forme validée est vide."}), 400

    created = False
    try:
        if data.get("lexicon_id"):
            lexicon = CentralLexiconStore.get_manageable_lexicon(str(data["lexicon_id"]), current_user)
            if lexicon is None:
                return jsonify({"error": "Lexique introuvable ou non géré."}), 404
        else:
            name = str(data.get("new_lexicon_name") or "").strip()
            if not name:
                return jsonify({"error": "Choisissez un lexique existant ou nommez le nouveau."}), 400
            groups = _promote_groups_view()
            group_id = str(data.get("group_id") or "") or (groups[0]["id"] if len(groups) == 1 else "")
            allow_global = current_user.has_role(Role.ADMIN)
            if not group_id and not allow_global:
                return jsonify({"error": "Précisez le groupe du nouveau lexique."}), 400
            lexicon = CentralLexiconStore.create_lexicon(
                current_user, name=name, group_id=group_id or None, allow_global=allow_global)
            created = True
            audit_log(AuditAction.LEXICON_CREATE, target_type="lexicon", target_id=lexicon.id, target_label=name)

        entry = CentralLexiconStore.add_or_update_entry(
            lexicon, current_user,
            term=term,
            variants=data.get("variants") or [],
            category=str(data.get("category") or "mot suspect"),
            priority=str(data.get("priority") or "normale"),
            source="session_promote",
        )
    except CentralLexiconValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except CentralLexiconAccessError as exc:
        return jsonify({"error": str(exc)}), 403

    audit_log(AuditAction.LEXICON_TERM_ADD, target_type="lexicon", target_id=lexicon.id,
              target_label=lexicon.name, details={"term": term, "from_job": job.id})
    return jsonify({"status": "ok", "lexicon": {"id": lexicon.id, "name": lexicon.name},
                    "created_lexicon": created, "entry_id": entry.id})


@web_bp.route("/api/jobs/<job_id>/lexicon", methods=["POST"])
@login_required
def api_lexicon(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    content_type = request.content_type or ""
    if "text/plain" in content_type or "text/csv" in content_type:
        text = request.data.decode("utf-8", errors="replace")
        input_summary = lexicon_text_audit_summary(text, source="session_import")
        saved_terms = LexiconManager.import_from_file(job, cfg["storage"]["jobs_dir"], text)
        audit_source = "text_import"
    else:
        data, _json_err = _json_body(list)
        if _json_err:
            return _json_err
        saved_terms = LexiconManager.save(job, cfg["storage"]["jobs_dir"], data)
        input_summary = {}
        audit_source = "json"

    central_entry_ids = [str(item.get("central_entry_id")) for item in saved_terms if item.get("central_entry_id")]
    if central_entry_ids:
        CentralLexiconStore.mark_entries_used(central_entry_ids)
        logger.info(
            "Lexique de session sauvegardé avec entrées centrales | job=%s central_entries=%d",
            job.id,
            len(set(central_entry_ids)),
        )

    advance_preprocessing_state(job.id, job.state)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])
    session_summary = lexicon_entries_audit_summary(saved_terms, source="session")
    central_lexicon_ids = sorted({
        str(item.get("central_lexicon_id"))
        for item in saved_terms
        if isinstance(item, dict) and item.get("central_lexicon_id")
    })
    audit_log(
        AuditAction.JOB_LEXICON_SAVE,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "source": audit_source,
            "central_entry_count": len(set(central_entry_ids)),
            "central_lexicon_count": len(central_lexicon_ids),
            "central_lexicon_ids": central_lexicon_ids[:20],
            **input_summary,
            **session_summary,
        },
    )
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/available-lexicons", methods=["GET"])
@login_required
def api_available_lexicons(job_id: str):
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
    return jsonify({
        "lexicons": [lexicon.to_dict(include_entries=True) for lexicon in lexicons],
    })


@web_bp.route("/api/resources/status", methods=["GET"])
@login_required
def api_resources_status():
    """État des ressources distantes pour le panneau frontale (mode dégradé inclus).

    Interroge /capabilities du nœud ; injoignable → reachable=False (la frontale
    affiche rouge, l'admission bascule en file/échec selon §7.2). Voir
    docs/SERVICE_RESSOURCES_GPU.md §7. Cache court par process pour éviter que
    chaque client web martèle directement le nœud de ressources.
    """
    cfg = get_config()
    from transcria.inference.client import InferenceUnavailable, build_client_from_config
    from transcria.inference.resource_status import remote_requirements, summarize_capabilities
    from transcria.queue.store import QueueStore
    from transcria.workflow.concurrency_profile import summarize_concurrency

    requirements = remote_requirements(cfg)
    cache_key = _resource_status_cache_key(cfg, requirements)
    ttl_s = _resource_status_cache_ttl_s(cfg)
    now_s = time.monotonic()
    cached = _get_cached_resource_status(cache_key, now_s)
    if cached is not None:
        return jsonify(cached)

    client = build_client_from_config(cfg)
    caps = None
    if client is not None:
        try:
            caps = client.capabilities()
        except InferenceUnavailable as exc:
            logger.info("Panneau ressources : nœud injoignable — %s", exc)
            caps = None
    summary = summarize_capabilities(caps)
    summary["requires_remote"] = sorted(requirements)
    # Profil de concurrence & goulot (C7/B8) : mesure best-effort côté frontale (c'est
    # l'orchestrateur qui exécute le workflow et connaît les durées par étape).
    try:
        queue_depth = QueueStore.count_by_status().get("waiting", 0)
    except Exception as exc:  # noqa: BLE001 — observabilité non bloquante
        logger.debug("Profondeur de file indisponible pour le profil de concurrence: %s", exc)
        queue_depth = 0
    summary["concurrency"] = summarize_concurrency(cfg, queue_depth=queue_depth)
    summary["cached"] = False
    _set_cached_resource_status(cache_key, summary, ttl_s, now_s)
    return jsonify(summary)


@web_bp.route("/api/jobs/<job_id>/selected-lexicons", methods=["POST"])
@login_required
def api_selected_lexicons(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    requested_ids = {str(item) for item in payload.get("selected_lexicon_ids", []) if str(item).strip()}
    lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
    available_ids = {lexicon.id for lexicon in lexicons}
    selected_ids = sorted(requested_ids.intersection(available_ids))
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.save_json("context/selected_lexicons.json", {
        "selected_lexicon_ids": selected_ids,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(
        "Sélection lexiques job sauvegardée | job=%s available=%d selected=%d ignored=%d",
        job.id,
        len(available_ids),
        len(selected_ids),
        len(requested_ids.difference(available_ids)),
    )
    audit_log(
        AuditAction.LEXICON_JOB_ASSIGN,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "selected_lexicon_ids": selected_ids,
            "requested_count": len(requested_ids),
            "selected_count": len(selected_ids),
            "ignored_count": len(requested_ids.difference(available_ids)),
            "raw_terms_logged": False,
        },
    )
    return jsonify({"status": "ok", "selected_lexicon_ids": selected_ids})


@web_bp.route("/api/jobs/<job_id>/lexicon/debug", methods=["GET"])
@login_required
def api_lexicon_debug(job_id: str):
    """Diagnostic lexique pour faciliter le débogage des affichages contextes.

    Retourne pour chaque terme :
    - les contextes bruts tels que sauvegardés dans session_lexicon.json
    - les contextes enrichis (audio_available, audio_start/end, réparation timecode)
    - les compteurs listened/playable
    - les éventuelles réparations de timecode détectées

    Réservé aux admin/operator. Ne modifie aucune donnée.
    """
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    raw_lexicon = LexiconManager.get(job, cfg["storage"]["jobs_dir"])
    summary_data = fs.load_json("summary/summary.json") or {}
    summary_segments = summary_data.get("segments") if isinstance(summary_data, dict) else []
    enriched_lexicon = _enrich_lexicon_context_audio(raw_lexicon, summary_segments)

    terms_debug = []
    for raw_term, enriched_term in zip(raw_lexicon, enriched_lexicon):
        raw_ctxs = raw_term.get("contexts") or []
        enr_ctxs = enriched_term.get("contexts") or []

        contexts_detail = []
        for i, (raw_ctx, enr_ctx) in enumerate(zip(raw_ctxs, enr_ctxs)):
            repair_notes = []
            if enr_ctx.get("timecode") and not raw_ctx.get("timecode"):
                repair_notes.append(
                    f"timecode réparé depuis la citation: {enr_ctx['timecode']!r}"
                )
            if enr_ctx.get("speaker") and not raw_ctx.get("speaker"):
                repair_notes.append(
                    f"speaker extrait depuis la citation: {enr_ctx['speaker']!r}"
                )
            if enr_ctx.get("quote") != raw_ctx.get("quote") and raw_ctx.get("quote"):
                repair_notes.append("citation nettoyée (timecode/speaker retirés)")
            if enr_ctx.get("audio_estimated_from_quote"):
                repair_notes.append("audio estimé depuis la citation sans timecode")

            contexts_detail.append({
                "index": i,
                "quote": enr_ctx.get("quote", ""),
                "timecode_raw": raw_ctx.get("timecode", ""),
                "timecode_used": enr_ctx.get("timecode", ""),
                "speaker": enr_ctx.get("speaker", ""),
                "audio_available": enr_ctx.get("audio_available", False),
                "audio_start": enr_ctx.get("audio_start"),
                "audio_end": enr_ctx.get("audio_end"),
                "audio_estimated_from_quote": bool(enr_ctx.get("audio_estimated_from_quote", False)),
                "listened": enr_ctx.get("listened", False),
                "repair_notes": repair_notes,
            })

        terms_debug.append({
            "id": raw_term.get("id"),
            "term": raw_term.get("term", ""),
            "source": raw_term.get("source", ""),
            "contexts_count": len(raw_ctxs),
            "contexts_playable": enriched_term.get("contexts_playable_count", 0),
            "contexts_listened": enriched_term.get("contexts_listened_count", 0),
            "contexts": contexts_detail,
        })

    filtered_path = fs.job_dir / "context" / "session_lexicon_filtered.json"
    filtered_lexicon = None
    if filtered_path.is_file():
        filtered_lexicon = fs.load_json("context/session_lexicon_filtered.json")

    summary = {
        "total_terms": len(raw_lexicon),
        "terms_with_contexts": sum(1 for t in raw_lexicon if t.get("contexts")),
        "total_contexts": sum(len(t.get("contexts") or []) for t in raw_lexicon),
        "total_playable": sum(t.get("contexts_playable_count", 0) for t in enriched_lexicon),
        "total_listened": sum(t.get("contexts_listened_count", 0) for t in enriched_lexicon),
        "filtered_lexicon_exists": filtered_path.is_file(),
        "filtered_terms": len(filtered_lexicon) if filtered_lexicon is not None else None,
    }

    logger.info(
        "Debug lexique consulté: job=%s terms=%d contexts=%d playable=%d",
        job.id,
        summary["total_terms"],
        summary["total_contexts"],
        summary["total_playable"],
    )
    return jsonify({"job_id": job_id, "summary": summary, "terms": terms_debug})


@web_bp.route("/api/jobs/<job_id>/speakers/detect", methods=["POST"])
@login_required
def api_speakers_detect(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    # run_speaker_detection est synchrone et publie SPEAKER_DETECTION_RUNNING le temps
    # de pyannote. Refuser un second appel concurrent évite deux runs GPU simultanés et
    # une course sur meeting_context.json (même classe que api_summary).
    if job.state == JobState.SPEAKER_DETECTION_RUNNING.value:
        return jsonify({"error": "Une détection des locuteurs est déjà en cours pour ce job."}), 409

    # Détection déjà en file sur le worker (frontal sans GPU) : ne pas relancer en synchrone.
    from transcria.queue.store import QUEUE_PAUSED, QUEUE_RUNNING, QUEUE_WAITING, QueueStore
    from transcria.services.job_executor import SPEAKER_MODE

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

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_speaker_detection(job, str(audio_path), cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/speakers/map", methods=["POST"])
@login_required
def api_speakers_map(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    mapping, _json_err = _json_body(dict)
    if _json_err:
        return _json_err
    from transcria.stt.speaker_detection import SpeakerDetector
    from transcria.workflow.runner import WorkflowRunner

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
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    if not cfg.get("voice_enrollment", {}).get("enabled", False):
        return jsonify({"error": "Voix enregistrées désactivées dans la configuration."}), 400

    from transcria.voice.matching import VoiceMatchingService

    service = VoiceMatchingService(cfg, device="cpu")
    result = service.match_job_speakers(job, current_user)
    status = 200 if result.get("available") else 409
    return jsonify(result), status


@web_bp.route("/api/jobs/<job_id>/process", methods=["POST"])
@login_required
def api_process(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    payload = request.get_json(silent=True) or {} if request.is_json else {}
    mode = payload.get("mode") or request.args.get("mode", "fast")
    if mode == "cancel":
        request_execution_cancel(job.id)
        if not is_execution_active(job) or get_execution_status(job) == "queued":
            from transcria.queue.store import QueueStore

            QueueStore.dequeue(job.id, status="cancelled")
            mark_execution_cancelled(job.id)
            JobStore.update_state(job.id, JobState.CANCELLED)
            return jsonify({"status": "cancelled"})
        return jsonify({"status": "cancel_requested"})

    from transcria.workflow import profiles

    processing_profile_id = payload.get("processing_profile_id") or request.args.get("processing_profile_id")
    try:
        # `mode` (legacy fast/quality) reste accepté ; un `processing_profile_id` explicite a
        # priorité. Le 2e membre est le mode d'exécution legacy de routage (Phase 4 le supprimera).
        profile, mode = profiles.resolve_request(processing_profile_id, mode)
    except (KeyError, ValueError):
        return jsonify({"error": f"Profil/mode de traitement invalide: {processing_profile_id or mode}"}), 400

    if mode == "quality" and not cfg.get("workflow", {}).get("enable_quality_mode", True):
        return jsonify({"error": "Le mode qualité est désactivé par la configuration"}), 400

    if not can_start_profile(job.state, profile):
        return jsonify(
            {
                "error": "Le job n'est pas prêt pour ce profil de traitement",
                "current_state": job.state,
                "processing_profile_id": profile.id,
            }
        ), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": get_execution_status(job)}), 409

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    priority = payload.get("priority", request.args.get("priority"))
    scheduled_at = None
    scheduled_at_raw = payload.get("scheduled_at") or request.args.get("scheduled_at")
    if scheduled_at_raw:
        from datetime import datetime

        try:
            scheduled_at = datetime.fromisoformat(str(scheduled_at_raw).replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "scheduled_at: format ISO 8601 invalide"}), 400

    if priority is not None and not _can_manage_queue_job(job):
        priority = None

    # Re-soumission utilisateur (ou nouveau run) : repartir d'un état de reprise PROPRE.
    # Les re-queues AUTOMATIQUES (vram_wait/deferred) préservent `completed_phases` — c'est
    # eux qui permettent la reprise ; ici c'est une intention utilisateur de (re)lancer.
    from transcria.workflow.resume import reset_resume_state

    reset_resume_state(JobStore, job.id)

    from transcria.services.pipeline_service import PipelineService

    vram_profile = PipelineService.estimate_profile_resources(cfg, profile)
    # Durée audio portée par l'entrée de file (DB) : la page File (non job-scoped) n'a pas
    # accès aux fichiers du job en mode frontale/nœud GPU — sans ça, l'estimation d'attente
    # serait vide en split. Cf. revue macro split.
    try:
        _aa = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).load_json("metadata/audio_analysis.json") or {}
        vram_profile["audio_seconds"] = float(_aa.get("duration_seconds") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort, l'attente retombe sur le fichier sinon
        pass
    try:
        result = executor.submit_process(
            job.id,
            str(audio_path),
            mode,
            priority=priority,
            scheduled_at=scheduled_at,
            vram_profile=vram_profile,
            processing_profile_id=profile.id,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": "active"}), 409
    JobStore.update(job.id, processing_mode=mode)
    if job.state != JobState.READY_TO_PROCESS.value:
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
    audit_log(
        action=AuditAction.JOB_ENQUEUE,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            # `processing_profile_id` = contrat produit ; `queue_mode`/`legacy_mode` = unité
            # d'exécution. On garde `mode` (= legacy) pour la compatibilité des consommateurs d'audit.
            "processing_profile_id": profile.id,
            "queue_mode": mode,
            "legacy_mode": mode,
            "mode": mode,
            "priority": result.get("priority"),
            "position": result.get("position"),
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        },
    )
    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "processing_profile_id": profile.id,
        "state": JobState.READY_TO_PROCESS.value,
        "execution_status": "queued",
        "queue_position": result.get("position"),
    }), 202


@web_bp.route("/api/jobs/<job_id>/status", methods=["GET"])
@login_required
def api_job_status(job_id: str):
    """Endpoint léger de polling — état courant du job pendant le traitement."""
    from transcria.workflow.progress import get_workflow_progress

    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    progress = get_workflow_progress(job)
    return jsonify({
        "state": job.state,
        "execution_status": get_execution_status(job) if is_execution_active(job) else "idle",
        "progress": progress,
        "eta": _live_eta(job, progress),
    })


def _live_eta(job, progress) -> dict | None:
    """ETA live du traitement (temps restant calibré machine) pour le polling de suivi.
    None si l'estimation n'est pas pertinente (pas en traitement, pas de progression)."""
    if not isinstance(progress, dict) or progress.get("step") != "processing":
        return None
    try:
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.profiles import profile_for_job
        from transcria.workflow.timing_service import estimate_remaining

        profile = profile_for_job(job)
        if profile is None:
            return None
        cfg = get_config()
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        audio_s = float((fs.load_json("metadata/audio_analysis.json") or {}).get("duration_seconds") or 0.0)
        if audio_s <= 0:
            return None
        return estimate_remaining(profile, audio_s, progress.get("percent"))
    except Exception:  # noqa: BLE001 — l'ETA ne doit jamais casser le polling
        return None


@web_bp.route("/api/profiles/availability", methods=["GET"])
@login_required
def api_profiles_availability():
    """Profils de traitement disponibles + profil recommandé (source unique pour le wizard)."""
    from transcria.workflow.profile_availability import compute_profiles_view

    return jsonify(compute_profiles_view(get_config()))


@web_bp.route("/api/jobs/<job_id>/profile", methods=["POST"])
@login_required
def api_set_profile(job_id: str):
    """Persiste le profil choisi à l'étape 1 (le wizard adapte alors ses étapes au profil).

    Distinct du lancement (`/process`) : ici on ne fait QUE mémoriser le contrat produit, sans
    enfiler le job. Le profil doit être valide ET réellement disponible sur cette installation.
    """
    from transcria.workflow import profiles
    from transcria.workflow.profile_availability import compute_profiles_view
    from transcria.workflow.transitions import set_processing_profile

    job, error_response = _get_job_for_api(job_id)
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


_REPROCESSABLE_STATES = {
    JobState.COMPLETED.value,
    JobState.QUALITY_CHECKED.value,
    JobState.EXPORT_READY.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
}


@web_bp.route("/api/jobs/<job_id>/reprocess", methods=["POST"])
@login_required
def api_reprocess(job_id: str):
    """Relance le traitement d'un job déjà terminé (lexique modifié, prompt mis à jour…)."""
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    if job.state not in _REPROCESSABLE_STATES:
        return jsonify({
            "error": "Le job ne peut pas être relancé dans son état actuel",
            "current_state": job.state,
        }), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Fichier audio introuvable"}), 400

    from transcria.workflow import profiles

    payload = request.get_json(silent=True) or {}
    processing_profile_id = payload.get("processing_profile_id")
    try:
        profile, mode = profiles.resolve_request(processing_profile_id, payload.get("mode", "fast"))
    except (KeyError, ValueError):
        return jsonify({"error": f"Profil/mode invalide: {processing_profile_id or payload.get('mode')}"}), 400

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    # Reprocess = run PROPRE (lexique/prompt modifiés) : vider l'état de reprise, sinon
    # le pipeline reprenable sauterait toutes les phases déjà faites → no-op silencieux.
    from transcria.workflow.resume import reset_resume_state

    reset_resume_state(JobStore, job.id)

    from transcria.services.pipeline_service import PipelineService

    vram_profile = PipelineService.estimate_profile_resources(cfg, profile)
    # Durée audio portée par l'entrée de file (DB) : la page File (non job-scoped) n'a pas
    # accès aux fichiers du job en mode frontale/nœud GPU — sans ça, l'estimation d'attente
    # serait vide en split. Cf. revue macro split.
    try:
        _aa = JobFilesystem(cfg["storage"]["jobs_dir"], job.id).load_json("metadata/audio_analysis.json") or {}
        vram_profile["audio_seconds"] = float(_aa.get("duration_seconds") or 0.0)
    except Exception:  # noqa: BLE001 — best-effort, l'attente retombe sur le fichier sinon
        pass
    try:
        result = executor.submit_process(
            job.id, str(audio_path), mode, vram_profile=vram_profile, processing_profile_id=profile.id
        )
    except TypeError as exc:
        # Compat (skew de version de l'exécuteur) : signature sans les kwargs récents.
        if "unexpected keyword argument" not in str(exc):
            raise
        result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409
    JobStore.update(job.id, processing_mode=mode)
    JobStore.update_state(job.id, JobState.READY_TO_PROCESS)

    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "processing_profile_id": profile.id,
        "reprocess": True,
    }), 202


@web_bp.route("/api/jobs/<job_id>/quality", methods=["POST"])
@login_required
def api_quality(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_quality_checks(job, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/export", methods=["POST"])
@login_required
def api_export(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.build_export(job, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/download/srt", methods=["GET"])
@login_required
def api_download_srt(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
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
def api_download_package(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
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
        if stale:
            from transcria.exports.package_builder import PackageBuilder
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
    job, error_response = _get_job_for_api(job_id)
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
def api_download_docx(job_id: str):
    import logging
    _log = logging.getLogger(__name__)
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    from transcria.exports.docx_report import generate_docx_report
    from transcria.workflow.profiles import profile_for_job

    # Le DOCX n'est un livrable que si le profil le promet (docx_level != none). Un profil SRT
    # (srt_express/srt_locuteurs) ne doit pas voir un DOCX généré à la demande → 404 propre.
    # Job legacy / sans profil → comportement complet (DOCX disponible).
    profile = profile_for_job(job)
    if profile is not None and profile.docx_level == "none":
        abort(404)

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
    docx_path = fs.job_dir / "exports" / f"rapport_{safe_title}.docx"

    try:
        generate_docx_report(job.id, cfg["storage"]["jobs_dir"], docx_path)
    except Exception:
        _log.exception("Échec génération rapport DOCX pour le job %s", job.id)
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


# ── Chat d'affinage des livrables (post-workflow) ─────────────────────────────
# L'utilisateur discute avec la LLM locale sur un job TERMINÉ, puis applique une
# demande validée : la phase `refine` (mode d'étape de la file) édite les artefacts
# texte sous garde-fous et versionne. Le web ne fait qu'écrire la demande
# (refine/request.json) et enfiler — l'exécution est asynchrone (l'UI poll /chat).

_REFINE_READY_STATES = (JobState.COMPLETED.value, JobState.EXPORT_READY.value)


def _refine_store(cfg, job_id: str):
    from transcria.workflow.refine_store import RefineStore

    return RefineStore(jobs_dir=cfg["storage"]["jobs_dir"], job_id=job_id)


def _refine_running(job) -> bool:
    """Un tour d'affinage est-il en file/en cours d'exécution pour ce job ?"""
    from transcria.services.job_executor import REFINE_MODE
    from transcria.workflow.transitions import EXECUTION_ACTIVE_STATUSES

    execution = job.get_extra_data().get("execution", {}) or {}
    return execution.get("mode") == REFINE_MODE and execution.get("status") in EXECUTION_ACTIVE_STATUSES


@web_bp.route("/api/jobs/<job_id>/refine", methods=["POST"])
@login_required
def api_refine_submit(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    refine_cfg = cfg.get("workflow", {}).get("refine_chat", {}) or {}
    if refine_cfg.get("enabled", True) is False:
        return jsonify({"error": "Chat d'affinage désactivé"}), 404
    if job.state not in _REFINE_READY_STATES:
        return jsonify({"error": "Le chat d'affinage n'est disponible qu'une fois le traitement terminé"}), 409

    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "discuss")
    if kind not in ("discuss", "apply"):
        return jsonify({"error": "kind invalide (discuss ou apply)"}), 400
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message vide"}), 400
    max_chars = int(refine_cfg.get("max_message_chars", 4000))
    if len(message) > max_chars:
        return jsonify({"error": f"Message trop long (max {max_chars} caractères)"}), 400

    store = _refine_store(cfg, job.id)
    if store.has_active_request() or _refine_running(job):
        return jsonify({"error": "Une demande d'affinage est déjà en cours pour ce job"}), 409

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    from transcria.services.job_executor import REFINE_MODE

    store.write_request(kind=kind, message=message)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    # L'audio n'est pas utilisé par l'affinage (il peut être purgé sur un job terminé).
    audio_path = fs.get_original_audio_path()
    submit = executor.submit_process(job.id, str(audio_path or ""), REFINE_MODE)
    if not submit.get("accepted", True):
        store.consume_request()  # pas de demande fantôme qui bloquerait les suivantes
        return jsonify({"error": "Le job est déjà dans la file de traitement"}), 409

    audit_log(
        AuditAction.JOB_REFINE_REQUEST, target_type="job", target_id=job.id,
        target_label=job.title, details={"kind": kind, "chars": len(message)},
    )
    return jsonify({"accepted": True, "kind": kind}), 202


@web_bp.route("/api/jobs/<job_id>/refine/chat", methods=["GET"])
@login_required
def api_refine_chat(job_id: str):
    """Endpoint de polling unique du panneau : tours + busy + versions + options."""
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    from transcria.exports.docx_report import _RENDER_SECTIONS, _THEMES

    store = _refine_store(cfg, job.id)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    refine_cfg = cfg.get("workflow", {}).get("refine_chat", {}) or {}
    return jsonify({
        "enabled": refine_cfg.get("enabled", True) is not False and job.state in _REFINE_READY_STATES,
        "turns": store.load_turns(),
        "busy": store.has_active_request() or _refine_running(job),
        "versions": store.list_versions(),
        "render_options": fs.load_json("context/render_options.json") or {},
        "themes": sorted(_THEMES),
        "sections": list(_RENDER_SECTIONS),
    })


@web_bp.route("/api/jobs/<job_id>/refine/render-options", methods=["POST"])
@login_required
def api_refine_render_options(job_id: str):
    """Options de rendu déterministes SANS LLM (thème/sections) — effet immédiat.

    Le DOCX étant régénéré à chaque téléchargement, écrire les options suffit ;
    le ZIP est reconstruit pour rester cohérent. Un snapshot de version est pris
    (restauration possible comme pour une application LLM).
    """
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    if job.state not in _REFINE_READY_STATES:
        return jsonify({"error": "Options disponibles une fois le traitement terminé"}), 409

    from transcria.exports.docx_report import _sanitize_render_options

    cleaned = _sanitize_render_options(request.get_json(silent=True) or {})
    if not cleaned:
        return jsonify({"error": "Aucune option de rendu valide (theme / sections)"}), 400

    store = _refine_store(cfg, job.id)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    version = store.snapshot_artifacts([
        fs.job_dir / "context" / "meeting_context.json",
        fs.job_dir / "metadata" / "transcription_corrigee.srt",
        fs.job_dir / "context" / "render_options.json",
    ])
    fs.save_json("context/render_options.json", cleaned)
    try:
        from transcria.exports.package_builder import PackageBuilder

        PackageBuilder(cfg).build_package(job)
    except Exception:
        logger.warning("Options de rendu : reconstruction du package échouée (best-effort) — job=%s",
                       job.id, exc_info=True)
    store.append_turn(role="system", kind="render_options",
                      text=f"Options de rendu mises à jour (version v{version} enregistrée).")
    audit_log(AuditAction.JOB_REFINE_REQUEST, target_type="job", target_id=job.id,
              target_label=job.title, details={"kind": "render_options", "options": cleaned})
    return jsonify({"applied": cleaned, "version": version})


@web_bp.route("/api/jobs/<job_id>/refine/revert", methods=["POST"])
@login_required
def api_refine_revert(job_id: str):
    """Restaure un snapshot pris AVANT une application (retour arrière utilisateur)."""
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "version invalide"}), 400
    if version < 1:
        return jsonify({"error": "version invalide"}), 400

    store = _refine_store(cfg, job.id)
    restored = store.restore_version(version)
    if not restored:
        return jsonify({"error": f"Version v{version} introuvable"}), 404
    try:
        from transcria.exports.package_builder import PackageBuilder

        PackageBuilder(cfg).build_package(job)
    except Exception:
        logger.warning("Revert : reconstruction du package échouée (best-effort) — job=%s",
                       job.id, exc_info=True)
    store.append_turn(role="system", kind="revert",
                      text=f"Version v{version} restaurée ({', '.join(restored)}).")
    audit_log(AuditAction.JOB_REFINE_REVERT, target_type="job", target_id=job.id,
              target_label=job.title, details={"version": version, "restored": restored})
    return jsonify({"restored": restored, "version": version})


@web_bp.route("/api/jobs/<job_id>/audio/excerpt", methods=["GET"])
@login_required
def api_audio_excerpt(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
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
    resolved = _resolve_context_audio_range(timecode, quote, segments)  # type: ignore[arg-type]
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
    job, error_response = _get_job_for_api(job_id)
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
    job, error_response = _get_job_for_api(job_id)
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



@web_bp.route("/system")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def system_status():
    cfg = get_config()
    from transcria.diagnostics.system_status import get_system_status

    status = get_system_status()
    # Page consciente du RÔLE (docs/archive/REFONTE_UI.md lot D) : une frontale CPU-only
    # n'affiche pas de panneaux GPU locaux trompeurs ; le backend de stockage des
    # fichiers de jobs et sa volumétrie sont visibles ici.
    runtime_role = current_app.config.get("TRANSCRIA_ROLE", "all")
    storage_backend = artifact_store.backend_name(cfg)
    storage_stats = None
    if storage_backend == "pg":
        try:
            storage_stats = artifact_store.store_stats()
        except Exception:
            logger.exception("Volumétrie du magasin de fichiers indisponible")
    return render_template(
        "dashboard_status.html", status=status, app_config=cfg,
        runtime_role=runtime_role, storage_backend=storage_backend, storage_stats=storage_stats,
    )


def _render_config_form(config_yaml: str, config_path: str, validation_errors: list[str] | None = None,
                        status: int = 200, values: dict | None = None):
    from transcria.web import prompt_files
    from transcria.web.i18n import select_locale

    cfg_now = ConfigService.get_singleton()
    if values is None:
        values = display_values(cfg_now, CONFIG_FORM_SECTIONS)
    return render_template(
        "admin_config.html",
        prompts=prompt_files.load_prompts(cfg_now, select_locale()),
        scripts=prompt_files.load_scripts(cfg_now),
        config_yaml=config_yaml,
        config_path=config_path,
        system_info=ConfigService.detect_system(),
        validation_errors=validation_errors or [],
        sections=CONFIG_FORM_SECTIONS,
        values=values,
    ), status


@web_bp.route("/admin/config", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()

    if request.method == "POST" and request.form.get("_mode") == "form":
        partial = build_partial_config(request.form, CONFIG_FORM_SECTIONS)
        partial = restore_masked_secrets(partial, cfg, CONFIG_FORM_SECTIONS)
        merged = _deep_merge(cfg, partial)
        ok, errors, warnings = ConfigService.save_if_valid(merged, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(_("%(n)s erreur(s) de validation. Sauvegarde annulée.", n=len(errors)), "error")
            config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
            return _render_config_form(config_yaml, config_path, errors, 400, values=display_values(merged, CONFIG_FORM_SECTIONS))

        flash(_("Réglages sauvegardés."), "success")
        audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label=Path(config_path).name)
        cfg = ConfigService.get_singleton()

    elif request.method == "POST" and request.form.get("_mode") == "prompts":
        # Édition des prompts LLM : liste FERMÉE de fichiers connus (prompt_files),
        # garde non-vide + backup .bak — voir docs/archive/REFONTE_UI.md.
        from transcria.web import prompt_files
        from transcria.web.i18n import select_locale

        prompt_lang = select_locale()
        saved = 0
        current_prompts = prompt_files.load_prompts(cfg, prompt_lang)
        for spec in prompt_files.PROMPT_FILES:
            submitted = request.form.get(f"prompt-{spec['name']}")
            if submitted is None:
                continue
            current = next((p["content"] for p in current_prompts
                            if p["name"] == spec["name"]), "")
            if submitted.replace("\r\n", "\n") == current:
                continue
            ok, message = prompt_files.save_prompt(cfg, spec["name"], submitted, prompt_lang)
            flash(message, "success" if ok else "error")
            if ok:
                saved += 1
                audit_log(AuditAction.CONFIG_EDIT, target_type="prompt",
                          target_label=spec["filename"])
        if saved == 0:
            flash(_("Aucun prompt modifié."), "info")

    elif request.method == "POST":
        raw_yaml = request.form.get("config_yaml", "")
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            flash(_("YAML invalide : %(e)s", e=exc), "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        if not isinstance(loaded, dict):
            flash(_("La configuration doit être un objet YAML racine."), "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        loaded = _restore_masked_config_secrets(loaded, cfg)
        loaded = _deep_merge(cfg, loaded)
        ok, errors, warnings = ConfigService.save_if_valid(loaded, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(_("%(n)s erreur(s) de validation. Sauvegarde annulée.", n=len(errors)), "error")
            return _render_config_form(raw_yaml, config_path, errors, 400)

        flash(_("Configuration sauvegardée dans %(p)s.", p=config_path), "success")
        audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label=Path(config_path).name)
        cfg = ConfigService.get_singleton()

    config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
    return _render_config_form(config_yaml, config_path)


@web_bp.route("/admin/maintenance")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance():
    from transcria.maintenance.restore import describe_restore
    from transcria.maintenance.schedule import backup_schedule_status
    from transcria.web.maintenance_service import MaintenanceService

    cfg = ConfigService.get_singleton()
    try:
        status = backup_schedule_status()  # lecture seule (systemctl is-enabled/is-active)
    except Exception:  # noqa: BLE001 — statut best-effort, jamais bloquant pour la page
        status = {"unit": "transcria-backup.timer", "enabled": "", "active": ""}
    archives = MaintenanceService.list_archives(cfg)
    previews: dict = {}
    for entry in archives:  # aperçu léger (manifeste seul) pour la restauration
        archive = MaintenanceService.resolve_archive(cfg, entry["name"])
        if archive is not None:
            try:
                previews[entry["name"]] = describe_restore(archive)
            except Exception:  # noqa: BLE001 — un manifeste illisible ne casse pas la page
                previews[entry["name"]] = None
    return render_template(
        "admin_maintenance.html",
        archives=archives,
        previews=previews,
        backup_dir=str(MaintenanceService.backup_dir(cfg)),
        schedule=(cfg.get("maintenance", {}) or {}).get("schedule", {}) or {},
        schedule_status=status,
    )


@web_bp.route("/admin/maintenance/schedule", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_schedule():
    from transcria.maintenance.schedule import (
        BackupSchedule,
        install_backup_schedule,
        remove_backup_schedule,
    )

    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    action = request.form.get("action")
    try:
        if action == "enable":
            schedule = BackupSchedule.from_config(cfg, config_path)
            install_backup_schedule(schedule)
            audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
                      target_label=f"planification activée (OnCalendar={schedule.on_calendar})")
            flash(_("Sauvegarde planifiée activée (cadence %(c)s).", c=schedule.on_calendar), "success")
        elif action == "disable":
            remove_backup_schedule()
            audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
                      target_label="planification désactivée")
            flash(_("Sauvegarde planifiée désactivée."), "success")
    except Exception as exc:  # noqa: BLE001 — surface l'échec systemd à l'opérateur
        flash(_("Échec de la planification : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_maintenance"))


@web_bp.route("/admin/maintenance/restore", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_restore():
    from transcria.maintenance.backup import verify_backup
    from transcria.maintenance.restore_service import request_restore
    from transcria.maintenance.schedule import BackupSchedule
    from transcria.web.maintenance_service import MaintenanceService

    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    name = (request.form.get("name") or "").strip()

    # Confirmation FORTE : case cochée + ressaisie exacte du nom (opération destructive).
    if request.form.get("acknowledge") != "on":
        flash(_("Confirmation requise : la restauration remplace les données et redémarre le service."), "error")
        return redirect(url_for("web.admin_maintenance"))
    if (request.form.get("confirm_name") or "").strip() != name:
        flash(_("Le nom ressaisi ne correspond pas à l'archive — restauration annulée."), "error")
        return redirect(url_for("web.admin_maintenance"))

    archive = MaintenanceService.resolve_archive(cfg, name)  # anti path-traversal
    if archive is None:
        abort(404)
    problems = verify_backup(archive)
    if problems:
        flash(_("Archive invalide — restauration refusée : ") + " ; ".join(problems), "error")
        return redirect(url_for("web.admin_maintenance"))

    schedule = BackupSchedule.from_config(cfg, config_path)
    try:
        request_restore(
            install_dir=schedule.install_dir, python_bin=schedule.python_bin,
            config_path=schedule.config_path, env_file=schedule.env_file,
            archive_name=archive.name,
        )
        audit_log(AuditAction.MAINTENANCE_BACKUP_RESTORE, target_type="maintenance",
                  target_label=archive.name)
        flash(_("Restauration lancée. Le service va s'arrêter, restaurer, puis redémarrer — "
                "reconnectez-vous dans une minute environ."), "success")
    except Exception as exc:  # noqa: BLE001 — surface l'échec de déclenchement à l'opérateur
        flash(_("Échec du déclenchement de la restauration : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_maintenance"))


def _models_view():
    from transcria.models_catalog import catalog_with_status

    cfg = ConfigService.get_singleton()
    total_vram_mb = int(ConfigService.detect_system().get("total_vram_mb") or 0) or None
    return catalog_with_status(cfg, total_vram_mb=total_vram_mb)


@web_bp.route("/admin/models")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models():
    import os

    from transcria.models_catalog import resolve_hf_home, resolve_models_dir
    from transcria.models_download import read_progress

    view = _models_view()
    hf_home, models_dir = resolve_hf_home(), resolve_models_dir()
    for item in view["items"]:
        item["progress"] = read_progress(item["spec"], hf_home=hf_home, models_dir=models_dir)
    return render_template("admin_models.html", view=view, has_token=bool(os.environ.get("HF_TOKEN")))


@web_bp.route("/admin/models/download", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_download():
    import os

    from transcria.models_catalog import resolve_hf_home, resolve_models_dir
    from transcria.models_download import check_space, start_download

    role = request.form.get("role")
    token = (request.form.get("token") or "").strip() or os.environ.get("HF_TOKEN") or None
    spec = next((it["spec"] for it in _models_view()["items"] if it["spec"].role == role), None)
    if spec is None:
        abort(404)
    if spec.gated and not token:
        flash(_("« %(l)s » est un modèle *gated* : un token HuggingFace est requis "
                "(et l'acceptation de sa licence sur huggingface.co).", l=spec.label), "error")
        return redirect(url_for("web.admin_models"))
    ok, msg = check_space(spec, hf_home=resolve_hf_home(), models_dir=resolve_models_dir())
    if not ok:
        flash(_("Téléchargement refusé — ") + msg, "error")
        return redirect(url_for("web.admin_models"))
    start_download(spec, token=token)
    flash(_("Téléchargement de « %(l)s » lancé en arrière-plan.", l=spec.label), "success")
    return redirect(url_for("web.admin_models"))


@web_bp.route("/admin/models/activate", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_activate():
    # Relie le téléchargement au SERVING : bascule le profil llama.cpp sur le GGUF téléchargé
    # (scripts/switch_arbitrage_llm.sh régénère le wrapper + met à jour services.arbitrage_script).
    import os
    import subprocess

    from transcria.models_catalog import resolve_models_dir

    item = next((it for it in _models_view()["items"] if it["spec"].role == "arbitrage_llm"), None)
    if item is None or not item["spec"].tier:
        abort(404)
    if not item["present"]:
        flash(_("Téléchargez d'abord ce modèle avant de l'activer."), "error")
        return redirect(url_for("web.admin_models"))

    tier_arg = f"{item['spec'].tier}gb"
    env = {**os.environ, "MODELS_DIR": str(resolve_models_dir())}
    try:
        result = subprocess.run(["bash", "scripts/switch_arbitrage_llm.sh", tier_arg],
                                capture_output=True, text=True, env=env, cwd=os.getcwd(), timeout=120)
        if result.returncode == 0:
            flash(_("Modèle LLM activé (profil %(t)s). Redémarrez le service pour l'appliquer : "
                    "sudo systemctl restart transcria", t=tier_arg), "success")
        else:
            flash(_("Échec de l'activation : ") + ((result.stderr or result.stdout).strip()[:300]), "error")
    except Exception as exc:  # noqa: BLE001 — surface l'échec du script à l'opérateur
        flash(_("Échec de l'activation : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_models"))


@web_bp.route("/admin/models/progress/<role>")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_progress(role: str):
    # Polled toutes les ~2 s : lecture du statut auto-suffisant, SANS détection GPU ni catalogue.
    from transcria.models_catalog import resolve_hf_home, resolve_models_dir
    from transcria.models_download import progress_by_role

    return jsonify(progress_by_role(role, hf_home=resolve_hf_home(), models_dir=resolve_models_dir()))


@web_bp.route("/admin/maintenance/backup", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_backup():
    from transcria.web.maintenance_service import MaintenanceService

    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    exclude_audio = request.form.get("exclude_audio") == "on"
    keep = request.form.get("keep", type=int) or 0
    MaintenanceService.start_backup(cfg, config_path, exclude_audio=exclude_audio, keep=keep)
    audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
              target_label="backup manuel")
    flash(_("Sauvegarde lancée en arrière-plan. Rafraîchissez la page dans quelques instants."), "success")
    return redirect(url_for("web.admin_maintenance"))


@web_bp.route("/admin/maintenance/backup/<name>/download")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_download(name: str):
    from transcria.web.maintenance_service import MaintenanceService

    cfg = ConfigService.get_singleton()
    archive = MaintenanceService.resolve_archive(cfg, name)  # anti path-traversal
    if archive is None:
        abort(404)
    return send_file(archive, as_attachment=True, download_name=archive.name,
                     mimetype="application/gzip")


@web_bp.route("/api/system/status")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def api_system_status():
    from transcria.diagnostics.system_status import get_system_status

    return jsonify(get_system_status())


@web_bp.route("/jobs/<job_id>/delete", methods=["POST"])
@login_required
@requires(Permission.DELETE_JOBS)
def delete_job(job_id: str):
    cfg = get_config()
    if not cfg.get("security", {}).get("allow_job_delete", True):
        abort(403)

    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)

    assert job is not None
    audit_log(AuditAction.JOB_DELETE, target_type="job", target_id=job.id, target_label=job.title)
    from transcria.workflow.agent_workspace import resolve_agent_work_root
    JobService.delete(job.id, cfg["storage"]["jobs_dir"], agent_work_dir=resolve_agent_work_root(cfg))
    flash(_("Traitement supprimé."), "info")
    return redirect(url_for("web.index"))

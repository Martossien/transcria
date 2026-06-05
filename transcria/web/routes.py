import copy
import logging
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
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
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
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.lexicon_audit import lexicon_entries_audit_summary, lexicon_text_audit_summary
from transcria.context.meeting_context import MEETING_TYPES, TYPE_SPECIFIC_FIELDS, MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.database import db
from transcria.integrations.dashboard_client import DashboardClient
from transcria.integrations.srt_editor_link import SrtEditorLink
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.services.config_service import ConfigService
from transcria.services.job_executor import get_job_executor
from transcria.services.job_service import JobService
from transcria.workflow.states import WorkflowState
from transcria.workflow.transitions import (
    advance_preprocessing_state,
    can_start_processing,
    get_execution_status,
    is_execution_active,
    mark_execution_cancelled,
    request_execution_cancel,
)

web_bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)

MEETING_TYPES_LIST = MEETING_TYPES
TYPE_SPECIFIC_FIELDS_JSON = __import__("json").dumps(TYPE_SPECIFIC_FIELDS, ensure_ascii=False)
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

    parsed = OpenCodeRunner._parse_structured_summary(summary_text)
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
    flash("Nouveau traitement créé.", "success")
    return redirect(url_for("web.job_wizard", job_id=job.id))


@web_bp.route("/jobs/<job_id>")
@login_required
def job_wizard(job_id: str):
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
    srt_content = fs.load_text("metadata/transcription.srt") or ""

    return render_template(
        "job_wizard.html",
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
        audio_scene=audio_scene,
        processing_diagnostic=_processing_diagnostic_view(transcription_metadata, transcription_segments),
        quality_report=quality_report,
        srt_content=srt_content,
        meeting_types=MEETING_TYPES_LIST,
        type_specific_fields_json=TYPE_SPECIFIC_FIELDS_JSON,
        lexicon_categories=LEXICON_CATEGORIES,
        lexicon_priorities=LEXICON_PRIORITIES,
        voice_enrollment_enabled=bool(cfg.get("voice_enrollment", {}).get("enabled", False)),
        srt_editor_url=SrtEditorLink.resolve_public_url(cfg, request.host),
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

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    quality_report = fs.load_json("quality/quality_report.json") or {}
    review_points = fs.load_json("quality/review_points.json") or []
    srt_content = fs.load_text("metadata/transcription.srt") or ""
    has_package = (fs.job_dir / "exports" / f"transcrIA_job_{job.id}.zip").is_file()
    safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
    has_docx = (fs.job_dir / "exports" / f"rapport_{safe_title}.docx").is_file()

    return render_template(
        "job_result.html",
        job=job,
        quality_report=quality_report,
        review_points=review_points,
        srt_content=srt_content,
        has_package=has_package,
        has_docx=has_docx,
        srt_editor_url=SrtEditorLink.resolve_public_url(cfg, request.host),
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

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)  # type: ignore[arg-type]
    result = runner.run_summary(job, str(audio_path), cfg)
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

    hint = _normalize_speaker_hint(request.get_json() or {})
    JobStore.update_extra_data(job.id, lambda extra: {**extra, "speaker_hint": hint})
    return jsonify({"status": "ok", "speaker_hint": hint})


@web_bp.route("/api/jobs/<job_id>/context", methods=["POST"])
@login_required
def api_context(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    data = request.get_json() or {}
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

    data = request.get_json() or []
    ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], data)
    audit_log(AuditAction.JOB_PARTICIPANTS_SAVE, target_type="job", target_id=job.id, target_label=job.title)
    if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
        JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
    return jsonify({"status": "ok"})


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
        data = request.get_json() or []
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

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

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

    mapping = request.get_json() or {}
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

    if mode not in ("fast", "quality"):
        return jsonify({"error": f"Mode de traitement invalide: {mode}"}), 400

    if mode == "quality" and not cfg.get("workflow", {}).get("enable_quality_mode", True):
        return jsonify({"error": "Le mode qualité est désactivé par la configuration"}), 400

    if not can_start_processing(job.state):
        return jsonify(
            {
                "error": "Le job n'est pas prêt pour le traitement",
                "current_state": job.state,
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

    from transcria.services.pipeline_service import PipelineService

    vram_profile = PipelineService.estimate_job_vram(cfg, mode)
    try:
        result = executor.submit_process(
            job.id,
            str(audio_path),
            mode,
            priority=priority,
            scheduled_at=scheduled_at,
            vram_profile=vram_profile,
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
    return jsonify({
        "state": job.state,
        "execution_status": get_execution_status(job) if is_execution_active(job) else "idle",
        "progress": get_workflow_progress(job),
    })


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

    mode = (request.get_json(silent=True) or {}).get("mode", "fast")
    if mode not in ("fast", "quality"):
        return jsonify({"error": f"Mode invalide: {mode}"}), 400

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409
    JobStore.update(job.id, processing_mode=mode)
    JobStore.update_state(job.id, JobState.READY_TO_PROCESS)

    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
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


@web_bp.route("/api/jobs/<job_id>/push-to-editor", methods=["POST"])
@login_required
def api_push_to_editor(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    srt_content = fs.load_text("metadata/transcription.srt")

    if audio_path is None or srt_content is None:
        return jsonify({"error": "Audio ou SRT manquant"}), 400

    editor_url = SrtEditorLink.get_server_url(cfg)
    editor = SrtEditorLink(editor_url)
    audio_result = editor.push_audio(str(audio_path))
    if "error" in audio_result:
        return jsonify({"error": "Échec envoi audio", "detail": audio_result}), 500

    project_id = audio_result.get("project_id", "")
    srt_result = editor.push_srt(project_id, srt_content) if project_id else {"error": "pas de project_id"}
    audit_log(
        AuditAction.JOB_EXTERNAL_PUSH,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "destination": "srt_editor",
            "destination_origin": _audit_origin_from_url(editor_url),
            "audio_sent": "error" not in audio_result,
            "srt_sent": "error" not in srt_result,
            "project_id_present": bool(project_id),
        },
    )
    return jsonify({"audio": audio_result, "srt": srt_result, "editor_url": SrtEditorLink.resolve_public_url(cfg, request.host)})


@web_bp.route("/system")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def system_status():
    cfg = get_config()
    db_url = cfg.get("services", {}).get("dashboard_llm_url", "http://127.0.0.1:5001")
    client = DashboardClient(db_url)
    status = client.get_system_status()
    return render_template("dashboard_status.html", status=status, app_config=cfg)


def _render_config_form(config_yaml: str, config_path: str, validation_errors: list[str] | None = None, status: int = 200):
    return render_template(
        "admin_config.html",
        config_yaml=config_yaml,
        config_path=config_path,
        system_info=ConfigService.detect_system(),
        validation_errors=validation_errors or [],
    ), status


@web_bp.route("/admin/config", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()

    if request.method == "POST":
        raw_yaml = request.form.get("config_yaml", "")
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            flash(f"YAML invalide : {exc}", "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        if not isinstance(loaded, dict):
            flash("La configuration doit être un objet YAML racine.", "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        loaded = _restore_masked_config_secrets(loaded, cfg)
        loaded = _deep_merge(cfg, loaded)
        ok, errors, warnings = ConfigService.save_if_valid(loaded, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(f"{len(errors)} erreur(s) de validation. Sauvegarde annulée.", "error")
            return _render_config_form(raw_yaml, config_path, errors, 400)

        flash(f"Configuration sauvegardée dans {config_path}.", "success")
        audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label=config_path)
        cfg = ConfigService.get_singleton()

    config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
    return _render_config_form(config_yaml, config_path)


@web_bp.route("/api/system/status")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def api_system_status():
    cfg = get_config()
    db_url = cfg.get("services", {}).get("dashboard_llm_url", "http://127.0.0.1:5001")
    client = DashboardClient(db_url)
    return jsonify(client.get_system_status())


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
    JobService.delete(job.id, cfg["storage"]["jobs_dir"])
    flash("Traitement supprimé.", "info")
    return redirect(url_for("web.index"))

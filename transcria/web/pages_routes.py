"""Pages HTML du portail : accueil, wizard de job, résultat, système, suppression.

Vague A2 — routes et helpers de vue déplacés tels quels depuis ``web/routes.py``.
Les helpers privés de ce module ne servent que ses propres pages ; ce qui est
partagé avec d'autres modules de routes vit dans ``job_access`` / ``lexicon_views``
/ ``request_helpers``.
"""
import json
import logging
import math

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.audit.store import AuditStore
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, requires
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.context.central_lexicon_service import merge_lexicon_entries, prefilter_lexicon_entries_for_display
from transcria.context.central_lexicon_store import CentralLexiconStore
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.meeting_context import MeetingContextManager
from transcria.context.meeting_type_catalog import localized_type_display, meeting_type_names, type_specific_fields
from transcria.context.meeting_type_store import MeetingTypeStore
from transcria.context.participants import ParticipantsManager
from transcria.diagnostics.system_status import get_system_status
from transcria.gpu.opencode_runner import _SUMMARY_MARKERS, OpenCodeRunner, resolve_output_language, summary_markers
from transcria.i18n import select_locale
from transcria.jobs import artifact_store
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.services.job_service import JobService
from transcria.web.blueprint import web_bp
from transcria.web.job_access import require_job_access
from transcria.web.lexicon_views import (
    LEXICON_DISPLAY_MAX_ENTRIES,
    enrich_lexicon_context_audio,
    promote_allowed,
    promote_groups_view,
    promote_lexicons_view,
)
from transcria.web.request_helpers import clean_job_title
from transcria.workflow.agent_workspace import resolve_agent_work_root
from transcria.workflow.profile_availability import compute_profiles_view, compute_wizard_layout
from transcria.workflow.profiles import get_profile, is_profile, profile_for_job
from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.states import WorkflowState
from transcria.workflow.timing_service import estimate_total_with_human

logger = logging.getLogger(__name__)


def _effective_srt(fs) -> str | None:
    """SRT « livrable » : la version corrigée (correction LLM, affinage) prime sur le brut.

    Même préférence que ``/download/srt`` — les aperçus à l'écran et l'éditeur SRT
    doivent montrer ce que l'utilisateur téléchargera, pas la transcription brute.
    """
    return fs.load_text("metadata/transcription_corrigee.srt") or fs.load_text("metadata/transcription.srt")


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
        "ok": _("Son exploitable"),
        "suspect": _("À surveiller"),
        "degrade": _("Son difficile"),
    }
    level_classes = {
        "ok": "success",
        "suspect": "warning",
        "degrade": "danger",
    }
    flag_labels = {
        "audio_tres_faible": _("volume très faible"),
        "audio_faible": _("volume faible"),
        "snr_faible": _("bruit de fond présent"),
        "bande_etroite": _("voix peu détaillée"),
        "clipping_detecte": _("saturation détectée"),
        "risque_transcription_non_fiable": _("vérification renforcée utile"),
        "squim_stoi_faible": _("intelligibilité réduite"),
        "squim_pesq_faible": _("qualité perceptive faible"),
        "squim_sisdr_faible": _("distorsion présente"),
        "dnsmos_ovrl_faible": _("qualité globale faible"),
        "rt60_eleve": _("réverbération marquée"),
        "c50_faible": _("clarté faible"),
        "codec_artefact": _("bande téléphonique (codec)"),
        "overlap": _("voix superposées"),
        "sig_lt_bak": _("parole peu nette"),
    }
    flags = [str(flag) for flag in preflight.get("flags", []) if flag]
    reasons = [flag_labels.get(flag, flag.replace("_", " ")) for flag in flags]
    if "audio_tres_faible" in flags and "risque_transcription_non_fiable" in flags:
        message = _("Le volume est très faible. La transcription sera probablement peu fiable — une relecture attentive est indispensable.")
    else:
        message = {
            "ok": _("Les caractéristiques audio ne montrent pas de risque majeur."),
            "suspect": _("La transcription reste possible, mais certains passages pourront demander une vérification."),
            "degrade": _("Le fichier est exploitable, avec un risque plus élevé sur certains mots ou passages."),
        }.get(level, _("Diagnostic audio disponible."))

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
                "text": _("Bruit de fond dominant — un débruitage peut aider (BAK %(bak)s < SIG %(sig)s).", bak=bak, sig=sig),
            }
        if sig < bak:
            return {
                "class": "warning",
                "text": _("Parole elle-même dégradée — vérification renforcée conseillée (SIG %(sig)s < BAK %(bak)s).", sig=sig, bak=bak),
            }
    if "codec_artefact" in flags:
        return {"class": "info", "text": _("Bande passante de type téléphonique détectée (codec).")}
    return None


def _recover_summary_speaker_hints(fs: JobFilesystem, meeting: dict) -> dict:
    """Récupère les champs participants LLM si un ancien parsing les a manqués."""
    if meeting.get("speaker_roles_llm") or meeting.get("participants_detectes"):
        return meeting

    summary_text = meeting.get("summary_llm") or fs.load_text("summary/summary.md") or ""
    if not summary_text.strip():
        return meeting

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


def _wizard_synthese_prefill(meeting: dict) -> str:
    """Valeur initiale du champ « Résumé » de l'étape 4 : l'édition manuelle si elle existe,
    sinon UNIQUEMENT la section synthèse du résumé LLM — jamais tout le markdown brut (méta,
    participants, termes, bloc JSON de données structurées).

    Le marqueur de section (« ## Synthèse » / « ## Summary »…) est choisi selon la langue du
    job, avec repli sur TOUS les marqueurs connus : robuste pour les jobs dont la langue n'a
    pas (encore) été persistée. Auparavant, le template testait « ## Synthèse » en dur → un
    résumé anglais affichait le markdown complet dans le champ éditable.
    """
    edited = str((meeting or {}).get("summary") or "").strip()
    if edited:
        return edited
    llm = str((meeting or {}).get("summary_llm") or "")
    if not llm.strip():
        return ""

    headings = [summary_markers(meeting.get("language"))["summary_heading"]]
    headings += [m["summary_heading"] for m in _SUMMARY_MARKERS.values()]
    for heading in headings:
        if heading in llm:
            return llm.split(heading, 1)[1].split("\n##", 1)[0].strip()
    return llm.strip()


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
    title = clean_job_title(request.form.get("title"))
    job = JobStore.create_job(owner_id=current_user.id, title=title)
    flash(_("Nouveau traitement créé."), "success")
    return redirect(url_for("web.job_wizard", job_id=job.id))


@web_bp.route("/jobs/<job_id>")
@login_required
def job_wizard(job_id: str):
    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    require_job_access(job, current_user)
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
    profiles_view = compute_profiles_view(cfg, select_locale())
    selected_profile = profile_for_job(job)

    # Types de réunion : intégrés + personnalisés VISIBLES DU PROPRIÉTAIRE du job
    # (même règle que les lexiques : un admin qui consulte voit le catalogue du
    # propriétaire, pas le sien). Champs spécifiques fusionnés pour le JS étape 4.
    _owner = UserStore.get_by_id(job.owner_id)
    if _owner is not None:
        builtin_meeting_types, custom_meeting_types, merged_type_fields = (
            MeetingTypeStore.merged_catalog_for_user(_owner)
        )
    else:  # propriétaire supprimé : catalogue intégré seul (le wizard reste servable)
        builtin_meeting_types = meeting_type_names()
        custom_meeting_types = []
        merged_type_fields = type_specific_fields()
    # Affichage traduit des types intégrés dans la locale de l'INTERFACE (axe A). Le
    # `name` reste la CLÉ (value de l'<option>, posté en meeting_type, lookups/comparaisons) ;
    # seule l'étiquette visible est localisée. Custom = déjà dans la langue de l'auteur.
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
    # Pré-sélection du sélecteur de langue (étape 3) : langue explicite du job, sinon la langue
    # RÉSOLUE (locale du propriétaire = langue d'interface choisie). Sans ça, le <select> ne
    # sélectionnait aucune option quand meeting.language était vide → le navigateur retombait
    # sur la 1re (Français) → l'utilisateur enregistrait « fr » et forçait des livrables FR.
    if not meeting.get("language"):
        meeting["language"] = resolve_output_language(job)
    # Pré-remplissage du champ « Résumé » (étape 4) : synthèse SEULE, langue-aware (le template
    # testait « ## Synthèse » en dur → markdown brut affiché pour un résumé anglais).
    synthese_prefill = _wizard_synthese_prefill(meeting)
    session_lexicon = LexiconManager.get(job, cfg["storage"]["jobs_dir"])
    central_lexicons, initial_lexicon, central_lexicon_display = _central_lexicon_context(job, fs, session_lexicon, meeting)
    summary_segments = summary_data.get("segments") if isinstance(summary_data, dict) else []
    lexicon = enrich_lexicon_context_audio(initial_lexicon, summary_segments)
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
        synthese_prefill=synthese_prefill,
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
        promote_lexicons=promote_lexicons_view(),
        promote_groups=promote_groups_view(),
        promote_allowed=promote_allowed(),
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
    require_job_access(job, current_user)
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
    profile = profile_for_job(job)
    has_docx = profile is None or profile.docx_level != "none"
    has_package = profile is None or profile.zip_level != "none"
    # §5.2 : marqueur posé par l'éditeur SRT quand la synthèse LLM n'a pas été
    # resynchronisée après une édition du verbatim (levé par apply_refine). Ne PAS
    # le conditionner au profil : un job SRT peut avoir une synthèse (autostart du
    # wizard) et son DOCX verbatim la mentionne aussi — attrapé par le test UI réel.
    summary_stale = fs.load_json("metadata/summary_stale.json") or {}

    return render_template(
        "job_result.html",
        job=job,
        quality_report=quality_report,
        review_points=review_points,
        srt_content=srt_content,
        has_package=has_package,
        has_docx=has_docx,
        summary_stale=bool(summary_stale),
    )


@web_bp.route("/system")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def system_status():
    cfg = get_config()
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


@web_bp.route("/jobs/<job_id>/delete", methods=["POST"])
@login_required
@requires(Permission.DELETE_JOBS)
def delete_job(job_id: str):
    cfg = get_config()
    if not cfg.get("security", {}).get("allow_job_delete", True):
        abort(403)

    job = JobStore.get_by_id(job_id)
    require_job_access(job, current_user)

    assert job is not None
    audit_log(AuditAction.JOB_DELETE, target_type="job", target_id=job.id, target_label=job.title)
    JobService.delete(job.id, cfg["storage"]["jobs_dir"], agent_work_dir=resolve_agent_work_root(cfg))
    flash(_("Traitement supprimé."), "info")
    return redirect(url_for("web.index"))

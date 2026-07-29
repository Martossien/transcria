"""Schéma de config — plateforme : serveur, stockage, GPU, services, sécurité, maintenance, i18n, live."""
from __future__ import annotations

from transcria.config.checks.base import (  # noqa: F401
    ValidationResult,
    _check_bool,
    _check_int_range,
    _check_optional_number,
    _check_optional_positive_int,
    _check_port_value,
    _check_regex_list,
    _check_regex_string,
    _check_str,
    _check_time_string,
)


def _check_required_keys(cfg: dict, r: ValidationResult) -> None:
    for key in ("server", "storage", "auth", "services", "models", "workflow", "security"):
        if key not in cfg or cfg[key] is None:
            r.add_error(f"Section '{key}' manquante ou null")

def _check_server(srv: dict, r: ValidationResult) -> None:
    _check_str(srv, "host", "server.host", r)
    _check_int_range(srv, "port", "server.port", 1, 65535, r)
    _check_bool(srv, "debug", "server.debug", r)

def _check_storage(sto: dict, r: ValidationResult) -> None:
    _check_str(sto, "jobs_dir", "storage.jobs_dir", r)
    _check_str(sto, "database_url", "storage.database_url", r)
    # Optionnel (défaut calculé = tempdir système) : valider uniquement si fourni.
    if "agent_work_dir" in sto:
        _check_str(sto, "agent_work_dir", "storage.agent_work_dir", r)
    backend = sto.get("shared_backend")
    if backend is not None and backend not in ("fs", "pg"):
        r.add_error("storage.shared_backend: doit être 'fs' ou 'pg'")
    if backend == "pg" and not str(sto.get("database_url", "")).startswith("postgresql"):
        r.add_error("storage.shared_backend: 'pg' requiert une base PostgreSQL (storage.database_url)")

def _check_voice_enrollment(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("voice_enrollment: doit être un objet YAML")
        return

    for key in (
        "enabled",
        "require_active_consent",
        "delete_source_audio_after_embedding",
        "allow_global_profiles",
        "require_explicit_job_group_for_multi_group_users",
    ):
        _check_bool(cfg, key, f"voice_enrollment.{key}", r)
    _check_str(cfg, "storage_dir", "voice_enrollment.storage_dir", r)

    embedding = cfg.get("embedding", {})
    if not isinstance(embedding, dict):
        r.add_error("voice_enrollment.embedding: doit être un objet YAML")
    else:
        _check_str(embedding, "backend", "voice_enrollment.embedding.backend", r)
        backend = embedding.get("backend")
        if isinstance(backend, str) and backend not in {"pyannote"}:
            r.add_error("voice_enrollment.embedding.backend: doit valoir pyannote")
        _check_str(embedding, "model_id", "voice_enrollment.embedding.model_id", r)
        revision = embedding.get("model_revision")
        if revision is not None and not isinstance(revision, str):
            r.add_error("voice_enrollment.embedding.model_revision: doit être une chaîne ou null")
        expected_dim = embedding.get("expected_dim")
        if expected_dim is not None:
            _check_int_range(embedding, "expected_dim", "voice_enrollment.embedding.expected_dim", 1, 100000, r)
        normalization = embedding.get("normalization")
        if normalization != "l2":
            r.add_error("voice_enrollment.embedding.normalization: doit valoir l2")
        _check_bool(embedding, "exclude_overlap", "voice_enrollment.embedding.exclude_overlap", r)
        _check_optional_number(embedding, "min_speech_duration_s", "voice_enrollment.embedding.min_speech_duration_s", r)
        _check_optional_number(embedding, "min_segment_duration_s", "voice_enrollment.embedding.min_segment_duration_s", r)
        _check_int_range(embedding, "max_segments_per_speaker", "voice_enrollment.embedding.max_segments_per_speaker", 1, 1000, r)

    matching = cfg.get("matching", {})
    if not isinstance(matching, dict):
        r.add_error("voice_enrollment.matching: doit être un objet YAML")
    else:
        _check_bool(matching, "enabled_after_summary", "voice_enrollment.matching.enabled_after_summary", r)
        _check_bool(matching, "stale_profiles_are_matchable", "voice_enrollment.matching.stale_profiles_are_matchable", r)
        for key in ("suggestion_threshold", "high_confidence_threshold", "min_top2_margin"):
            _check_optional_number(matching, key, f"voice_enrollment.matching.{key}", r)
        _check_int_range(matching, "max_candidates_per_speaker", "voice_enrollment.matching.max_candidates_per_speaker", 1, 20, r)
        low = matching.get("suggestion_threshold")
        high = matching.get("high_confidence_threshold")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
            r.add_error("voice_enrollment.matching.suggestion_threshold doit être <= high_confidence_threshold")

    consent = cfg.get("consent", {})
    if not isinstance(consent, dict):
        r.add_error("voice_enrollment.consent: doit être un objet YAML")
    else:
        _check_str(consent, "current_form_version", "voice_enrollment.consent.current_form_version", r)
        _check_bool(consent, "allow_expiration", "voice_enrollment.consent.allow_expiration", r)
        if consent.get("validity_days") is not None:
            _check_int_range(consent, "validity_days", "voice_enrollment.consent.validity_days", 1, 36500, r)
        _check_int_range(consent, "max_proof_size_mb", "voice_enrollment.consent.max_proof_size_mb", 1, 1024, r)
        values = consent.get("proof_allowed_extensions", [])
        if not isinstance(values, list) or not values:
            r.add_error("voice_enrollment.consent.proof_allowed_extensions: doit être une liste non vide")
        else:
            for i, value in enumerate(values):
                if not isinstance(value, str) or not value.strip():
                    r.add_error(f"voice_enrollment.consent.proof_allowed_extensions[{i}]: doit être une chaîne non vide")

    audit = cfg.get("audit", {})
    if audit:
        if not isinstance(audit, dict):
            r.add_error("voice_enrollment.audit: doit être un objet YAML")
        else:
            _check_bool(audit, "log_match_suggestions", "voice_enrollment.audit.log_match_suggestions", r)
            _check_bool(audit, "log_match_scores", "voice_enrollment.audit.log_match_scores", r)

def _check_gpu(gpu: dict, r: ValidationResult) -> None:
    _check_int_range(gpu, "cohere_vram_mb", "gpu.cohere_vram_mb", 1000, 100000, r)
    _check_int_range(gpu, "pyannote_vram_mb", "gpu.pyannote_vram_mb", 500, 100000, r)
    _check_int_range(gpu, "llm_vram_mb", "gpu.llm_vram_mb", 1000, 500000, r)
    _check_int_range(gpu, "granite_vram_mb", "gpu.granite_vram_mb", 1000, 100000, r)
    _check_int_range(gpu, "voxtral_vram_mb", "gpu.voxtral_vram_mb", 1000, 100000, r)
    _check_int_range(gpu, "moss_vram_mb", "gpu.moss_vram_mb", 1000, 100000, r)
    _check_int_range(gpu, "min_free_vram_mb", "gpu.min_free_vram_mb", 100, 50000, r)
    indices = gpu.get("llm_gpu_indices")
    if indices is not None:
        if not isinstance(indices, list) or not indices or not all(
            isinstance(i, int) and 0 <= i <= 63 for i in indices
        ):
            r.add_error("gpu.llm_gpu_indices: doit être une liste non vide d'index GPU (entiers ≥ 0), ou absent (= tous)")
        elif len(set(indices)) != len(indices):
            r.add_error("gpu.llm_gpu_indices: index GPU dupliqués")
    per_gpu = gpu.get("llm_vram_mb_per_gpu")
    if per_gpu is not None:
        if not isinstance(per_gpu, list) or not per_gpu or not all(
            isinstance(mb, int) and mb > 0 for mb in per_gpu
        ):
            r.add_error("gpu.llm_vram_mb_per_gpu: doit être une liste non vide de Mo (entiers > 0), ou absent (= parts égales)")
        elif isinstance(indices, list) and len(per_gpu) != len(indices):
            r.add_error("gpu.llm_vram_mb_per_gpu: doit avoir autant d'éléments que gpu.llm_gpu_indices")

def _check_services(svc: dict, r: ValidationResult) -> None:
    if "arbitrage_llm_port" in svc:
        _check_int_range(svc, "arbitrage_llm_port", "services.arbitrage_llm_port", 1, 65535, r)
    else:
        _check_int_range(svc, "qwen_port", "services.qwen_port", 1, 65535, r)
    if "llm_cleanup_ports" in svc:
        ports = svc.get("llm_cleanup_ports")
        if not isinstance(ports, list):
            r.add_error("services.llm_cleanup_ports: doit être une liste de ports")
        else:
            for i, port in enumerate(ports):
                _check_port_value(port, f"services.llm_cleanup_ports[{i}]", r)
    elif "vllm_port" in svc:
        _check_int_range(svc, "vllm_port", "services.vllm_port", 1, 65535, r)

    for key in ("arbitrage_script", "stop_script"):
        val = svc.get(key, "")
        if val is None or (isinstance(val, str) and val.strip() == ""):
            r.add_error(f"services.{key}: chemin de script non défini")

def _check_quality(quality: dict, r: ValidationResult) -> None:
    if not quality:
        return
    if not isinstance(quality, dict):
        r.add_error("quality: doit être un objet YAML")
        return
    markers = quality.get("asr_noise_markers", [])
    if markers is not None and not isinstance(markers, list):
        r.add_error("quality.asr_noise_markers: doit être une liste")
    elif isinstance(markers, list):
        for i, marker in enumerate(markers):
            if not isinstance(marker, str) or not marker.strip():
                r.add_error(f"quality.asr_noise_markers[{i}]: doit être une chaîne non vide")
    thresholds = quality.get("thresholds", {})
    if thresholds:
        if not isinstance(thresholds, dict):
            r.add_error("quality.thresholds: doit être un objet YAML")
        else:
            for key in (
                "no_speech_prob_threshold", "low_word_confidence_ratio",
                "low_word_confidence_min",
            ):
                _check_optional_number(thresholds, key, f"quality.thresholds.{key}", r)

def _check_security(sec: dict, r: ValidationResult) -> None:
    _check_int_range(sec, "retention_days", "security.retention_days", 0, 3650, r)
    _check_bool(sec, "allow_job_delete", "security.allow_job_delete", r)
    _check_bool(sec, "session_cookie_secure", "security.session_cookie_secure", r)
    _check_bool(sec, "behind_tls_proxy", "security.behind_tls_proxy", r)
    _check_bool(sec, "hsts_enabled", "security.hsts_enabled", r)
    _check_int_range(sec, "hsts_max_age_days", "security.hsts_max_age_days", 1, 3650, r)
    _check_bool(sec, "csrf_origin_check", "security.csrf_origin_check", r)
    _check_bool(sec, "csrf_tokens", "security.csrf_tokens", r)
    # CSP : valeurs miroir de transcria.web.csp.CSP_MODES (littéral ici — config = noyau,
    # n'importe pas la couche web ; verrouillé par test_security_hardening).
    if str(sec.get("csp", "off")).strip().lower() not in ("off", "report-only", "enforce"):
        r.add_error(f"security.csp='{sec.get('csp')}' invalide (attendu : off, report-only, enforce)")
    if bool(sec.get("hsts_enabled", False)) and not (bool(sec.get("behind_tls_proxy", False))
                                                     or bool(sec.get("session_cookie_secure", False))):
        r.add_warning("security.hsts_enabled=true sans behind_tls_proxy ni session_cookie_secure : "
                      "le HSTS n'est émis que sur une réponse HTTPS réelle — activez behind_tls_proxy "
                      "(derrière un proxy TLS) pour qu'il prenne effet.")
    _check_int_range(sec, "max_upload_size_mb", "security.max_upload_size_mb", 1, 102400, r)

    extensions = sec.get("allowed_upload_extensions", [])
    if not isinstance(extensions, list) or len(extensions) == 0:
        r.add_error(
            "security.allowed_upload_extensions doit être une liste non vide "
            "d'extensions (ex: ['.mp3', '.wav'])"
        )
    elif isinstance(extensions, list):
        for i, ext in enumerate(extensions):
            if not isinstance(ext, str) or not ext.startswith("."):
                r.add_error(
                    f"security.allowed_upload_extensions[{i}]='{ext}' invalide "
                    "(doit commencer par un point)"
                )

    # Documents joints au contexte du résumé (extraction texte).
    _check_int_range(sec, "max_document_size_mb", "security.max_document_size_mb", 1, 1024, r)
    _check_int_range(sec, "max_document_chars", "security.max_document_chars", 500, 200000, r)
    _check_int_range(sec, "max_documents_per_job", "security.max_documents_per_job", 1, 100, r)
    doc_extensions = sec.get("allowed_document_extensions", [])
    if not isinstance(doc_extensions, list) or len(doc_extensions) == 0:
        r.add_error(
            "security.allowed_document_extensions doit être une liste non vide "
            "d'extensions (ex: ['.pdf', '.docx'])"
        )
    else:
        for i, ext in enumerate(doc_extensions):
            if not isinstance(ext, str) or not ext.startswith("."):
                r.add_error(
                    f"security.allowed_document_extensions[{i}]='{ext}' invalide "
                    "(doit commencer par un point)"
                )

def _check_maintenance(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("maintenance: doit être un objet YAML")
        return
    if "backup_dir" in cfg:
        _check_str(cfg, "backup_dir", "maintenance.backup_dir", r)
    sched = cfg.get("schedule")
    if sched is None:
        return
    if not isinstance(sched, dict):
        r.add_error("maintenance.schedule: doit être un objet YAML")
        return
    if "enabled" in sched:
        _check_bool(sched, "enabled", "maintenance.schedule.enabled", r)
    if "exclude_audio" in sched:
        _check_bool(sched, "exclude_audio", "maintenance.schedule.exclude_audio", r)
    if "keep" in sched:
        _check_int_range(sched, "keep", "maintenance.schedule.keep", 0, 10_000, r)
    if "on_calendar" in sched:
        _check_str(sched, "on_calendar", "maintenance.schedule.on_calendar", r)
    if "purge_on_calendar" in sched:
        _check_str(sched, "purge_on_calendar", "maintenance.schedule.purge_on_calendar", r)

_KNOWN_LOCALES = {"fr", "en", "es", "de", "it", "pt", "nl"}

def _check_i18n(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("i18n: doit être un objet YAML")
        return
    available = cfg.get("available_locales")
    if available is not None:
        if not isinstance(available, list) or not all(isinstance(x, str) for x in available):
            r.add_error("i18n.available_locales: doit être une liste de chaînes (codes de langue)")
            available = None
        else:
            for code in available:
                if code not in _KNOWN_LOCALES:
                    r.add_warning(
                        f"i18n.available_locales: langue '{code}' inconnue "
                        f"(reconnues : {', '.join(sorted(_KNOWN_LOCALES))})"
                    )
    default = cfg.get("default_locale")
    if default is not None:
        if not isinstance(default, str):
            r.add_error("i18n.default_locale: doit être une chaîne (code de langue)")
        elif isinstance(available, list) and available and default not in available:
            r.add_error(
                f"i18n.default_locale '{default}' absent de i18n.available_locales "
                f"({', '.join(available)})"
            )

def _check_live(cfg: dict, r: ValidationResult) -> None:
    """Section `live` (temps réel & connecteurs) — tout opt-in, défaut OFF.

    Voir docs/TEMPS_REEL_REUNIONS.md. Seule la façade STT keystone est instruite
    ici ; les connecteurs par plateforme s'ajouteront comme sous-sections.
    """
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("live: doit être un objet YAML")
        return
    facade = cfg.get("facade")
    if facade is not None:
        if not isinstance(facade, dict):
            r.add_error("live.facade: doit être un objet YAML")
        else:
            _check_bool(facade, "enabled", "live.facade.enabled", r)
            if "max_sync_audio_mb" in facade:
                _check_int_range(facade, "max_sync_audio_mb",
                                 "live.facade.max_sync_audio_mb", 1, 500, r)
            if "max_sync_duration_s" in facade:
                _check_int_range(facade, "max_sync_duration_s",
                                 "live.facade.max_sync_duration_s", 1, 86400, r)
            if "idle_unload_s" in facade:
                _check_optional_number(facade, "idle_unload_s", "live.facade.idle_unload_s", r)


# Codes de langue reconnus (allowlist volontairement restreinte : on ne veut pas de locale
# fantaisiste dans le sélecteur). Étendre ici en même temps qu'on livre un catalogue.


def _check_connectors(cfg: dict, r: ValidationResult) -> None:
    """Section `connectors.meetings` (vague 3 réunions) — opt-in, validation minimale."""
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        r.add_error("connectors: doit être un objet YAML")
        return
    meetings = cfg.get("meetings")
    if meetings is None:
        return
    if not isinstance(meetings, dict):
        r.add_error("connectors.meetings: doit être un objet YAML")
        return
    _check_bool(meetings, "enabled", "connectors.meetings.enabled", r)
    usernames = meetings.get("runner_usernames")
    if usernames is not None and (not isinstance(usernames, list)
                                  or any(not isinstance(u, str) or not u.strip() for u in usernames)):
        r.add_error("connectors.meetings.runner_usernames: liste de noms d'utilisateurs non vides attendue")

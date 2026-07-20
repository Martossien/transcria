import re
from typing import Any


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def all_messages(self) -> list[str]:
        return self.errors + self.warnings

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


def validate_config(cfg: dict) -> ValidationResult:
    result = ValidationResult()
    _check_required_keys(cfg, result)
    _check_server(cfg.get("server", {}), result)
    _check_storage(cfg.get("storage", {}), result)
    _check_voice_enrollment(cfg.get("voice_enrollment", {}), result)
    _check_auth(cfg.get("auth", {}), result)
    _check_auth_backend(cfg.get("auth", {}) or {}, result)
    _check_gpu(cfg.get("gpu", {}), result)
    _check_services(cfg.get("services", {}), result)
    _check_models(cfg.get("models", {}), result, cfg)
    _check_cohere(cfg.get("cohere", {}), result)
    _check_cohere_tf5(cfg.get("cohere_tf5", {}), result)
    _check_whisper(cfg.get("whisper", {}), result)
    _check_granite(cfg.get("granite", {}), result)
    _check_voxtral(cfg.get("voxtral", {}), result)
    _check_kroko(cfg.get("kroko", {}), result)
    _check_moss(cfg.get("moss", {}), result)
    _check_workflow(cfg.get("workflow", {}), result)
    _check_stt_served_pools(cfg, result)
    _check_diarization(cfg.get("diarization", {}), result)
    _check_quality(cfg.get("quality", {}), result)
    _check_security(cfg.get("security", {}), result)
    _check_maintenance(cfg.get("maintenance", {}), result)
    _check_i18n(cfg.get("i18n", {}), result)
    return result


# Codes de langue reconnus (allowlist volontairement restreinte : on ne veut pas de locale
# fantaisiste dans le sélecteur). Étendre ici en même temps qu'on livre un catalogue.
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


_IMPLEMENTED_AUTH_BACKENDS = ("local",)  # étendu lot par lot (GESTION_IDENTITE.md)


def _check_auth_backend(auth: dict, r: ValidationResult) -> None:
    backend = str(auth.get("backend", "local") or "local").strip().lower()
    if backend not in _IMPLEMENTED_AUTH_BACKENDS:
        r.add_error(
            f"auth.backend='{backend}' non disponible. Implémentés : "
            f"{', '.join(_IMPLEMENTED_AUTH_BACKENDS)} (cf. docs/GESTION_IDENTITE.md) — "
            f"jamais de repli silencieux vers 'local'."
        )


def _check_auth(auth: dict, r: ValidationResult) -> None:
    _check_bool(auth, "enabled", "auth.enabled", r)
    _check_str(auth, "first_admin_username", "auth.first_admin_username", r)
    _check_str(auth, "first_admin_password", "auth.first_admin_password", r)
    pwd = auth.get("first_admin_password", "")
    if isinstance(pwd, str) and pwd in ("CHANGE-ME", "admin-change-me", ""):
        r.add_warning(
            "Sécurité : auth.first_admin_password utilise la valeur par défaut. "
            "Changez-la dès que possible."
        )


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


def _check_models(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    _check_stt_backend(mod, r, cfg)
    _check_summary_stt_backend(mod, r, cfg)
    _check_str(mod, "default_stt_model", "models.default_stt_model", r)
    _check_str(mod, "fallback_stt_model", "models.fallback_stt_model", r)
    _check_str(mod, "cohere_model_path", "models.cohere_model_path", r)
    cohere_revision = mod.get("cohere_model_revision")
    if cohere_revision is not None and not isinstance(cohere_revision, str):
        r.add_error("models.cohere_model_revision: doit être une chaîne ou null")
    _check_str(mod, "pyannote_model", "models.pyannote_model", r)

    stt_model = mod.get("stt_backend", "")
    cohere_path = mod.get("cohere_model_path", "")
    if isinstance(stt_model, str) and stt_model == "cohere" and not cohere_path:
        r.add_error(
            "models.cohere_model_path doit être renseigné quand le backend STT est 'cohere'"
        )


# Backends STT natifs acceptés. Liste LITTÉRALE à dessein : config/ est du noyau et
# n'importe pas le domaine stt (§8.2) — la cohérence avec stt/registry.py est verrouillée
# en CI par tests/contracts/test_stt_backend_contract.py (toute dérive casse la suite).
_VALID_STT_BACKENDS = frozenset({"cohere", "cohere_tf5", "whisper", "granite", "parakeet", "voxtral", "kroko", "moss"})


def _check_stt_backend(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    backend = mod.get("stt_backend", "cohere")
    if isinstance(backend, str) and backend in _VALID_STT_BACKENDS:
        return
    # Backend SERVI (runtimes C++, ex. qwen3asr/nemotron) : n'importe quel nom est
    # accepté s'il est ROUTÉ — url non vide dans inference.stt.backends.<nom>
    # (cf. docs/EXTERNAL_STT_RUNTIMES.md). Sans URL, l'erreur reste (le factory
    # retomberait silencieusement sur cohere — piège utilisateur).
    routed = (((cfg or {}).get("inference", {}) or {}).get("stt", {}) or {}).get("backends", {}) or {}
    if isinstance(backend, str) and str((routed.get(backend) or {}).get("url") or "").strip():
        return
    r.add_error(
        f"models.stt_backend='{backend}' invalide. "
        f"Valeurs acceptées: {', '.join(sorted(_VALID_STT_BACKENDS))} — ou un backend SERVI déclaré "
        f"avec une url dans inference.stt.backends.<nom> (runtimes audio.cpp/parakeet.cpp)"
    )


def _check_stt_served_pools(cfg: dict, r: ValidationResult) -> None:
    """Pools multi-instance (§2.9) : `inference.stt.backends.<nom>.extra_urls` doit
    être une liste d'URLs http(s) ; `resource_node.engines[].backend` doit référencer
    un backend servi déclaré (sinon l'instance ne serait jamais assurée)."""
    stt = ((cfg.get("inference", {}) or {}).get("stt", {}) or {})
    backends = stt.get("backends", {}) or {}
    for name, spec in backends.items():
        extra = (spec or {}).get("extra_urls")
        if extra is None:
            continue
        if not isinstance(extra, list) or not all(
            isinstance(u, str) and u.strip().startswith(("http://", "https://")) for u in extra
        ):
            r.add_error(
                f"inference.stt.backends.{name}.extra_urls doit être une liste d'URLs "
                f"http(s) (instances supplémentaires du même moteur)"
            )
    for entry in (cfg.get("resource_node", {}) or {}).get("engines", []) or []:
        declared = str((entry or {}).get("backend") or "").strip()
        if declared and declared not in backends:
            r.add_warning(
                f"resource_node.engines['{(entry or {}).get('name')}'].backend='{declared}' "
                f"ne correspond à aucun backend de inference.stt.backends — l'instance "
                f"ne sera jamais sollicitée par le pool client"
            )


def _check_summary_stt_backend(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    """`models.summary_stt_backend` : null (= backend principal) ou même règle que
    `stt_backend` (natif du registre, ou servi routé avec url)."""
    backend = mod.get("summary_stt_backend")
    if backend is None:
        return
    if isinstance(backend, str) and backend in _VALID_STT_BACKENDS:
        return
    routed = (((cfg or {}).get("inference", {}) or {}).get("stt", {}) or {}).get("backends", {}) or {}
    if isinstance(backend, str) and str((routed.get(backend) or {}).get("url") or "").strip():
        return
    r.add_error(
        f"models.summary_stt_backend='{backend}' invalide. "
        f"null (= backend principal), l'un de : {', '.join(sorted(_VALID_STT_BACKENDS))}, "
        f"ou un backend SERVI déclaré avec une url dans inference.stt.backends.<nom>"
    )


def _check_workflow(wf: dict, r: ValidationResult) -> None:
    _check_bool(wf, "enable_quick_summary", "workflow.enable_quick_summary", r)
    _check_bool(wf, "enable_speaker_detection", "workflow.enable_speaker_detection", r)
    _check_bool(wf, "enable_quality_mode", "workflow.enable_quality_mode", r)
    _check_progress_section(wf.get("progress", {}), r)
    _check_execution_section(wf.get("execution", {}), "workflow.execution", r)
    autostart = wf.get("summary_autostart", {})
    if isinstance(autostart, dict):
        _check_bool(autostart, "enabled", "workflow.summary_autostart.enabled", r)
    elif autostart:
        r.add_error("workflow.summary_autostart: doit être un objet YAML")
    vram_wait = wf.get("vram_wait", {})
    if isinstance(vram_wait, dict):
        _check_int_range(vram_wait, "max_wait_s", "workflow.vram_wait.max_wait_s", 0, 604800, r)
    elif vram_wait:
        r.add_error("workflow.vram_wait: doit être un objet YAML")
    _check_queue_section(wf.get("queue", {}), r)
    _check_scheduling_section(wf.get("scheduling", {}), r)
    _check_audio_quality(wf.get("audio_quality", {}), r)
    _check_quality_transcription(wf.get("quality_transcription", {}), r)
    _check_audio_preflight(wf.get("audio_preflight", {}), r)
    canonical = wf.get("audio_canonical_16k", {})
    if isinstance(canonical, dict):
        _check_bool(canonical, "enabled", "workflow.audio_canonical_16k.enabled", r)
    elif canonical:
        r.add_error("workflow.audio_canonical_16k: doit être un objet YAML")
    _check_segment_reliability(wf.get("segment_reliability", {}), r)
    _check_pyannote_chunking(wf.get("pyannote_chunking", {}), r)
    _check_vad_section(wf.get("vad", {}), r)
    _check_transcription_cleanup(wf.get("transcription_cleanup", {}), r)
    _check_multi_stt(wf.get("multi_stt", {}), r)
    _check_stt_hybrid(wf.get("stt_hybrid", {}), r)
    _check_audio_scene(wf.get("audio_scene", {}), r)
    _check_audio_scene_filter(wf.get("audio_scene_filter", {}), r)
    _check_audio_normalization(wf.get("audio_normalization", {}), r)
    _check_audio_denoise(wf.get("audio_denoise", {}), r)
    _check_source_separation(wf.get("source_separation", {}), r)
    _check_speaker_realignment(wf.get("speaker_realignment", {}), r)

    _check_llm_section(wf.get("summary_llm", {}), "workflow.summary_llm", r, is_summary=True)
    _check_llm_section(wf.get("arbitration_llm", {}), "workflow.arbitration_llm", r, is_summary=False)


def _check_progress_section(progress_cfg: dict, r: ValidationResult) -> None:
    if not progress_cfg:
        return
    if not isinstance(progress_cfg, dict):
        r.add_error("workflow.progress: doit être un objet YAML")
        return
    _check_bool(progress_cfg, "enabled", "workflow.progress.enabled", r)
    _check_optional_number(progress_cfg, "update_interval_s", "workflow.progress.update_interval_s", r)


def _check_whisper(whisper: dict, r: ValidationResult) -> None:
    if not whisper:
        return
    if not isinstance(whisper, dict):
        r.add_error("whisper: doit être un objet YAML")
        return
    _check_str(whisper, "model_size", "whisper.model_size", r)
    _check_str(whisper, "compute_type", "whisper.compute_type", r)
    _check_int_range(whisper, "cpu_threads", "whisper.cpu_threads", 1, 128, r)
    _check_int_range(whisper, "chunk_length_s", "whisper.chunk_length_s", 1, 300, r)
    _check_int_range(whisper, "beam_size", "whisper.beam_size", 1, 32, r)
    _check_int_range(whisper, "best_of", "whisper.best_of", 1, 32, r)
    for key in (
        "vad_filter", "word_timestamps", "condition_on_previous_text",
        "suppress_numerals", "collapse_repetition_loops",
    ):
        _check_bool(whisper, key, f"whisper.{key}", r)
    _check_optional_number(whisper, "no_speech_threshold", "whisper.no_speech_threshold", r)
    _check_optional_number(whisper, "compression_ratio_threshold", "whisper.compression_ratio_threshold", r)
    _check_optional_number(whisper, "log_prob_threshold", "whisper.log_prob_threshold", r)
    _check_optional_number(whisper, "hallucination_silence_threshold", "whisper.hallucination_silence_threshold", r)
    _check_optional_number(whisper, "repetition_penalty", "whisper.repetition_penalty", r)
    _check_int_range(whisper, "no_repeat_ngram_size", "whisper.no_repeat_ngram_size", 0, 20, r)
    _check_int_range(whisper, "repetition_loop_min_repeats", "whisper.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(whisper, "repetition_loop_max_phrase_words", "whisper.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(whisper, "repetition_loop_keep_repeats", "whisper.repetition_loop_keep_repeats", 1, 20, r)
    for key in ("hotwords", "initial_prompt"):
        val = whisper.get(key)
        if val is not None and not isinstance(val, str):
            r.add_error(f"whisper.{key}: doit être une chaîne ou null")
    lexicon_hotwords = whisper.get("lexicon_hotwords", {})
    if lexicon_hotwords is not None:
        if not isinstance(lexicon_hotwords, dict):
            r.add_error("whisper.lexicon_hotwords: doit être un objet YAML")
        else:
            _check_bool(lexicon_hotwords, "enabled", "whisper.lexicon_hotwords.enabled", r)
            _check_int_range(lexicon_hotwords, "max_terms", "whisper.lexicon_hotwords.max_terms", 1, 500, r)
            _check_int_range(lexicon_hotwords, "max_chars", "whisper.lexicon_hotwords.max_chars", 40, 10000, r)
            _check_int_range(lexicon_hotwords, "max_tokens", "whisper.lexicon_hotwords.max_tokens", 1, 224, r)
            _check_str(lexicon_hotwords, "prefix", "whisper.lexicon_hotwords.prefix", r)
            _check_str(lexicon_hotwords, "tokenizer_model", "whisper.lexicon_hotwords.tokenizer_model", r)
            priorities = lexicon_hotwords.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("whisper.lexicon_hotwords.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if priority not in allowed:
                        r.add_error(
                            f"whisper.lexicon_hotwords.priorities[{index}]: priorité invalide '{priority}'"
                        )
    forced = whisper.get("forced_alignment", {})
    if forced is not None:
        if not isinstance(forced, dict):
            r.add_error("whisper.forced_alignment: doit être un objet YAML")
        else:
            _check_bool(forced, "enabled", "whisper.forced_alignment.enabled", r)
            backend = forced.get("backend", "torchaudio_ctc")
            if backend != "torchaudio_ctc":
                r.add_error("whisper.forced_alignment.backend: doit valoir torchaudio_ctc")
            for key in ("bundle_name",):
                val = forced.get(key)
                if val is not None and not isinstance(val, str):
                    r.add_error(f"whisper.forced_alignment.{key}: doit être une chaîne ou null")
            _check_optional_number(forced, "max_segment_s", "whisper.forced_alignment.max_segment_s", r)


def _check_granite(granite: dict, r: ValidationResult) -> None:
    if not granite:
        return
    if not isinstance(granite, dict):
        r.add_error("granite: doit être un objet YAML")
        return
    _check_bool(granite, "enabled", "granite.enabled", r)
    _check_str(granite, "model_id", "granite.model_id", r)
    _check_str(granite, "torch_dtype", "granite.torch_dtype", r)
    dtype = granite.get("torch_dtype")
    if isinstance(dtype, str) and dtype not in {"bfloat16", "bf16", "float16", "fp16", "float32", "fp32"}:
        r.add_error("granite.torch_dtype: valeurs acceptées bfloat16, float16, float32")
    _check_int_range(granite, "chunk_length_s", "granite.chunk_length_s", 1, 600, r)
    _check_int_range(granite, "max_new_tokens", "granite.max_new_tokens", 1, 20000, r)
    _check_optional_number(granite, "max_new_tokens_per_second", "granite.max_new_tokens_per_second", r)
    _check_int_range(granite, "min_new_tokens", "granite.min_new_tokens", 1, 20000, r)
    max_new_tokens_per_second = granite.get("max_new_tokens_per_second")
    if (
        max_new_tokens_per_second is not None
        and not isinstance(max_new_tokens_per_second, bool)
        and isinstance(max_new_tokens_per_second, (int, float))
        and max_new_tokens_per_second <= 0
    ):
        r.add_error("granite.max_new_tokens_per_second: doit être strictement positif ou null")
    if (
        isinstance(granite.get("min_new_tokens"), int)
        and isinstance(granite.get("max_new_tokens"), int)
        and granite["min_new_tokens"] > granite["max_new_tokens"]
    ):
        r.add_error("granite.min_new_tokens: doit être inférieur ou égal à granite.max_new_tokens")
    _check_str(granite, "prompt_mode", "granite.prompt_mode", r)
    prompt_mode = granite.get("prompt_mode")
    if isinstance(prompt_mode, str) and prompt_mode not in {"asr_raw", "asr_punctuated", "keywords"}:
        r.add_error("granite.prompt_mode: valeurs acceptées asr_raw, asr_punctuated, keywords")
    for key in ("prompt_asr_raw", "prompt_asr_punctuated", "prompt_keywords"):
        _check_str(granite, key, f"granite.{key}", r)
    keywords = granite.get("keywords", [])
    if isinstance(keywords, str):
        pass
    elif isinstance(keywords, list):
        for index, keyword in enumerate(keywords):
            if not isinstance(keyword, str) or not keyword.strip():
                r.add_error(f"granite.keywords[{index}]: doit être une chaîne non vide")
    else:
        r.add_error("granite.keywords: doit être une chaîne ou une liste de chaînes")
    lexicon_keywords = granite.get("lexicon_keywords", {})
    if lexicon_keywords is not None:
        if not isinstance(lexicon_keywords, dict):
            r.add_error("granite.lexicon_keywords: doit être un objet YAML")
        else:
            _check_bool(lexicon_keywords, "enabled", "granite.lexicon_keywords.enabled", r)
            _check_int_range(lexicon_keywords, "max_terms", "granite.lexicon_keywords.max_terms", 1, 2000, r)
            priorities = lexicon_keywords.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("granite.lexicon_keywords.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if not isinstance(priority, str) or priority not in allowed:
                        r.add_error(
                            f"granite.lexicon_keywords.priorities[{index}]: valeurs acceptées critique, importante, normale"
                        )
    for key in ("fix_mistral_regex", "collapse_repetition_loops"):
        _check_bool(granite, key, f"granite.{key}", r)
    _check_int_range(granite, "repetition_loop_min_repeats", "granite.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(granite, "repetition_loop_max_phrase_words", "granite.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(granite, "repetition_loop_keep_repeats", "granite.repetition_loop_keep_repeats", 1, 20, r)


def _check_cohere(cohere: dict, r: ValidationResult) -> None:
    if not cohere:
        return
    if not isinstance(cohere, dict):
        r.add_error("cohere: doit être un objet YAML")
        return
    _check_optional_number(cohere, "chunk_length_s", "cohere.chunk_length_s", r)
    _check_int_range(cohere, "max_new_tokens", "cohere.max_new_tokens", 1, 4096, r)
    _check_bool(cohere, "punctuation", "cohere.punctuation", r)
    _check_optional_number(cohere, "repetition_penalty", "cohere.repetition_penalty", r)
    _check_int_range(cohere, "no_repeat_ngram_size", "cohere.no_repeat_ngram_size", 0, 20, r)
    _check_bool(cohere, "collapse_repetition_loops", "cohere.collapse_repetition_loops", r)
    _check_int_range(cohere, "repetition_loop_min_repeats", "cohere.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(cohere, "repetition_loop_max_phrase_words", "cohere.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(cohere, "repetition_loop_keep_repeats", "cohere.repetition_loop_keep_repeats", 1, 20, r)
    lexicon_biasing = cohere.get("lexicon_biasing", {})
    if lexicon_biasing is not None:
        if not isinstance(lexicon_biasing, dict):
            r.add_error("cohere.lexicon_biasing: doit être un objet YAML")
        else:
            _check_bool(lexicon_biasing, "enabled", "cohere.lexicon_biasing.enabled", r)
            _check_int_range(lexicon_biasing, "max_terms", "cohere.lexicon_biasing.max_terms", 1, 2000, r)
            _check_optional_number(lexicon_biasing, "boost", "cohere.lexicon_biasing.boost", r)
            boost = lexicon_biasing.get("boost")
            if isinstance(boost, (int, float)) and not isinstance(boost, bool) and (boost < 0 or boost > 2):
                r.add_error("cohere.lexicon_biasing.boost: doit être entre 0 et 2")
            _check_optional_number(lexicon_biasing, "start_boost", "cohere.lexicon_biasing.start_boost", r)
            start_boost = lexicon_biasing.get("start_boost")
            if (
                isinstance(start_boost, (int, float))
                and not isinstance(start_boost, bool)
                and (start_boost < 0 or start_boost > 1)
            ):
                r.add_error("cohere.lexicon_biasing.start_boost: doit être entre 0 et 1")
            _check_int_range(lexicon_biasing, "max_prefix_tokens", "cohere.lexicon_biasing.max_prefix_tokens", 1, 100, r)
            priorities = lexicon_biasing.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("cohere.lexicon_biasing.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if priority not in allowed:
                        r.add_error(
                            f"cohere.lexicon_biasing.priorities[{index}]: priorité invalide '{priority}'"
                        )


def _check_cohere_tf5(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("cohere_tf5: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "cohere_tf5.enabled", r)
    for key in ("tf5_site", "model_path"):
        _check_str(cfg, key, f"cohere_tf5.{key}", r)
    revision = cfg.get("model_revision")
    if revision is not None and not isinstance(revision, str):
        r.add_error("cohere_tf5.model_revision: doit être une chaîne ou null")
    _check_int_range(cfg, "timeout_s", "cohere_tf5.timeout_s", 1, 86400, r)
    _check_optional_number(cfg, "chunk_length_s", "cohere_tf5.chunk_length_s", r)
    _check_int_range(cfg, "max_new_tokens", "cohere_tf5.max_new_tokens", 1, 4096, r)
    _check_bool(cfg, "punctuation", "cohere_tf5.punctuation", r)
    _check_int_range(cfg, "batch_size", "cohere_tf5.batch_size", 1, 512, r)
    _check_optional_number(cfg, "repetition_penalty", "cohere_tf5.repetition_penalty", r)
    _check_int_range(cfg, "no_repeat_ngram_size", "cohere_tf5.no_repeat_ngram_size", 0, 20, r)
    _check_bool(cfg, "collapse_repetition_loops", "cohere_tf5.collapse_repetition_loops", r)
    _check_int_range(cfg, "repetition_loop_min_repeats", "cohere_tf5.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(cfg, "repetition_loop_max_phrase_words", "cohere_tf5.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(cfg, "repetition_loop_keep_repeats", "cohere_tf5.repetition_loop_keep_repeats", 1, 20, r)


def _check_quality_transcription(cfg: dict, r: ValidationResult) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.quality_transcription: doit être un objet YAML")
        return
    backend = cfg.get("force_stt_backend")
    if backend is not None and backend not in {"cohere", "cohere_tf5", "whisper", "granite", "parakeet"}:
        r.add_error("workflow.quality_transcription.force_stt_backend: doit valoir cohere, cohere_tf5, whisper, granite ou parakeet")
    _check_bool(cfg, "force_on_degraded_summary", "workflow.quality_transcription.force_on_degraded_summary", r)
    modes = cfg.get("enabled_for_modes", [])
    if not isinstance(modes, list):
        r.add_error("workflow.quality_transcription.enabled_for_modes: doit être une liste")
        return
    for mode in modes:
        if mode not in {"fast", "quality"}:
            r.add_error("workflow.quality_transcription.enabled_for_modes: valeurs acceptées fast, quality")
    degraded_levels = cfg.get("degraded_summary_levels", [])
    if not isinstance(degraded_levels, list):
        r.add_error("workflow.quality_transcription.degraded_summary_levels: doit être une liste")
        return
    for level in degraded_levels:
        if not isinstance(level, str) or not level.strip():
            r.add_error("workflow.quality_transcription.degraded_summary_levels: valeurs chaîne non vides attendues")


def _check_audio_quality(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_quality: doit être un objet YAML")
        return
    _check_bool(cfg, "force_quality_backend", "workflow.audio_quality.force_quality_backend", r)
    _check_bool(cfg, "scene_affects_quality_score", "workflow.audio_quality.scene_affects_quality_score", r)
    for key in ("degraded_levels", "suspect_levels"):
        values = cfg.get(key, [])
        if not isinstance(values, list):
            r.add_error(f"workflow.audio_quality.{key}: doit être une liste")
    for key in (
        "min_bit_rate", "min_sample_rate_hz", "max_non_latin_segments",
        "min_speech_ratio", "max_speech_ratio", "max_short_segment_ratio",
        "max_scene_music_ratio", "max_scene_noise_ratio",
        "max_scene_no_energy_ratio", "min_scene_speech_ratio",
        "max_scene_problem_segments",
    ):
        _check_optional_number(cfg, key, f"workflow.audio_quality.{key}", r)


def _check_audio_preflight(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_preflight: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.audio_preflight.enabled", r)
    _check_bool(cfg, "reuse_analysis", "workflow.audio_preflight.reuse_analysis", r)
    for key in (
        "frame_ms", "low_rms_threshold", "very_low_rms_threshold",
        "silence_rms_threshold", "low_snr_db_threshold",
        "narrowband_hz_threshold", "clipping_threshold",
        "clipping_ratio_threshold",
    ):
        _check_optional_number(cfg, key, f"workflow.audio_preflight.{key}", r)


def _check_segment_reliability(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.segment_reliability: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.segment_reliability.enabled", r)
    for key in (
        "detect_non_latin", "detect_generic_hallucinations", "degrade_on_text_flags",
    ):
        _check_bool(cfg, key, f"workflow.segment_reliability.{key}", r)
    for key in (
        "no_speech_prob_threshold", "low_word_confidence_ratio",
        "low_word_confidence_min", "micro_segment_s", "short_segment_s",
        "sparse_min_duration_s", "sparse_words_per_second",
    ):
        _check_optional_number(cfg, key, f"workflow.segment_reliability.{key}", r)
    if "non_latin_min_chars" in cfg:
        _check_int_range(cfg, "non_latin_min_chars", "workflow.segment_reliability.non_latin_min_chars", 1, 100, r)
    _check_regex_string(cfg, "non_latin_char_pattern", "workflow.segment_reliability.non_latin_char_pattern", r)
    _check_regex_list(
        cfg,
        "generic_hallucination_patterns",
        "workflow.segment_reliability.generic_hallucination_patterns",
        r,
    )


def _check_pyannote_chunking(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.pyannote_chunking: doit être un objet YAML")
        return
    _check_bool(cfg, "merge_micro_chunks", "workflow.pyannote_chunking.merge_micro_chunks", r)
    for key in (
        "micro_chunk_s", "micro_chunk_neighbor_gap_s", "isolated_min_chunk_s",
        "padding_s", "max_chunk_s", "min_chunk_s",
    ):
        _check_optional_number(cfg, key, f"workflow.pyannote_chunking.{key}", r)


def _check_vad_section(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.vad: doit être un objet YAML")
        return
    for key in (
        "enabled_summary", "enabled_final", "adaptive", "hysteresis_enabled",
        "auto_enable_final_on_degraded",
    ):
        _check_bool(cfg, key, f"workflow.vad.{key}", r)
    levels = cfg.get("auto_enable_final_levels", [])
    if not isinstance(levels, list):
        r.add_error("workflow.vad.auto_enable_final_levels: doit être une liste")
    else:
        for i, level in enumerate(levels):
            if not isinstance(level, str) or not level.strip():
                r.add_error(f"workflow.vad.auto_enable_final_levels[{i}]: doit être une chaîne non vide")
    for key in (
        "threshold", "threshold_low_quality", "threshold_high_noise",
        "threshold_final_degraded", "onset", "offset",
        "min_speech_duration_ms", "min_silence_duration_ms",
        "min_silence_duration_ms_low_quality", "speech_pad_ms",
        "speech_pad_ms_low_quality",
    ):
        _check_optional_number(cfg, key, f"workflow.vad.{key}", r)


def _check_transcription_cleanup(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.transcription_cleanup: doit être un objet YAML")
        return
    for key in (
        "enabled",
        "remove_subtitle_artifacts",
        "remove_obvious_hallucinations",
        "remove_non_latin_hallucinations",
        "remove_generic_hallucinations",
        "merge_short_segments",
    ):
        _check_bool(cfg, key, f"workflow.transcription_cleanup.{key}", r)
    for key in (
        "short_segment_max_s",
        "short_segment_max_words",
        "merge_gap_s",
        "merge_max_chars",
        "non_latin_min_ratio",
        "isolated_noise_artifact_max_s",
    ):
        _check_optional_number(cfg, key, f"workflow.transcription_cleanup.{key}", r)
    if "non_latin_min_chars" in cfg:
        _check_int_range(cfg, "non_latin_min_chars", "workflow.transcription_cleanup.non_latin_min_chars", 1, 100, r)
    _check_regex_string(cfg, "non_latin_char_pattern", "workflow.transcription_cleanup.non_latin_char_pattern", r)
    for key in (
        "subtitle_artifact_patterns",
        "subtitle_artifact_words",
        "generic_hallucination_patterns",
        "generic_hallucination_languages",
        "isolated_noise_artifact_words",
    ):
        values = cfg.get(key, [])
        if not isinstance(values, list):
            r.add_error(f"workflow.transcription_cleanup.{key}: doit être une liste")
        else:
            for i, value in enumerate(values):
                if not isinstance(value, str):
                    r.add_error(f"workflow.transcription_cleanup.{key}[{i}]: doit être une chaîne")


def _check_voxtral(voxtral: dict, r: ValidationResult) -> None:
    if not voxtral:
        return
    if not isinstance(voxtral, dict):
        r.add_error("voxtral: doit être un objet YAML")
        return
    _check_bool(voxtral, "enabled", "voxtral.enabled", r)
    _check_str(voxtral, "model_id", "voxtral.model_id", r)
    _check_str(voxtral, "torch_dtype", "voxtral.torch_dtype", r)
    dtype = voxtral.get("torch_dtype")
    if isinstance(dtype, str) and dtype not in {"bfloat16", "bf16", "float16", "fp16", "float32", "fp32"}:
        r.add_error("voxtral.torch_dtype: valeurs acceptées bfloat16, float16, float32")
    _check_int_range(voxtral, "chunk_length_s", "voxtral.chunk_length_s", 1, 600, r)
    _check_int_range(voxtral, "max_new_tokens", "voxtral.max_new_tokens", 1, 20000, r)
    _check_optional_number(voxtral, "max_new_tokens_per_second", "voxtral.max_new_tokens_per_second", r)
    _check_int_range(voxtral, "min_new_tokens", "voxtral.min_new_tokens", 1, 20000, r)
    max_new_tokens_per_second = voxtral.get("max_new_tokens_per_second")
    if (
        max_new_tokens_per_second is not None
        and not isinstance(max_new_tokens_per_second, bool)
        and isinstance(max_new_tokens_per_second, (int, float))
        and max_new_tokens_per_second <= 0
    ):
        r.add_error("voxtral.max_new_tokens_per_second: doit être strictement positif ou null")
    _check_bool(voxtral, "collapse_repetition_loops", "voxtral.collapse_repetition_loops", r)
    _check_int_range(voxtral, "repetition_loop_min_repeats", "voxtral.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(voxtral, "repetition_loop_max_phrase_words", "voxtral.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(voxtral, "repetition_loop_keep_repeats", "voxtral.repetition_loop_keep_repeats", 1, 20, r)


def _check_moss(moss: dict, r: ValidationResult) -> None:
    if not moss:
        return
    if not isinstance(moss, dict):
        r.add_error("moss: doit être un objet YAML")
        return
    _check_bool(moss, "enabled", "moss.enabled", r)
    if moss.get("enabled") and str(moss.get("moss_site") or "").startswith("/tmp"):
        r.add_warning(
            "moss.moss_site pointe sous /tmp (purgé au reboot) — le backend moss "
            "disparaîtra au redémarrage ; déplacer vers ./runtimes/moss_site et "
            "relancer `installer.cli moss-site --dir ./runtimes/moss_site` au besoin"
        )
    _check_str(moss, "model_path", "moss.model_path", r)
    _check_str(moss, "moss_site", "moss.moss_site", r)
    _check_int_range(moss, "timeout_s", "moss.timeout_s", 60, 86400, r)
    _check_int_range(moss, "max_new_tokens", "moss.max_new_tokens", 256, 65536, r)
    _check_int_range(moss, "single_pass_max_s", "moss.single_pass_max_s", 60, 7200, r)
    _check_optional_number(moss, "gap_alert_s", "moss.gap_alert_s", r)
    _check_bool(moss, "collapse_repetition_loops", "moss.collapse_repetition_loops", r)
    _check_int_range(moss, "repetition_loop_min_repeats", "moss.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(moss, "repetition_loop_max_phrase_words", "moss.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(moss, "repetition_loop_keep_repeats", "moss.repetition_loop_keep_repeats", 1, 20, r)


def _check_kroko(kroko: dict, r: ValidationResult) -> None:
    if not kroko:
        return
    if not isinstance(kroko, dict):
        r.add_error("kroko: doit être un objet YAML")
        return
    _check_bool(kroko, "enabled", "kroko.enabled", r)
    _check_str(kroko, "model_dir", "kroko.model_dir", r)
    _check_str(kroko, "repo_id", "kroko.repo_id", r)
    variant = kroko.get("variant")
    if variant is not None and str(variant) not in {"64", "128"}:
        r.add_error("kroko.variant: valeurs acceptées 64, 128")
    _check_int_range(kroko, "num_threads", "kroko.num_threads", 1, 128, r)
    method = kroko.get("decoding_method")
    if isinstance(method, str) and method not in {"greedy_search", "modified_beam_search"}:
        r.add_error("kroko.decoding_method: valeurs acceptées greedy_search, modified_beam_search")
    for key in ("tail_padding_s", "segment_max_gap_s", "segment_max_len_s"):
        _check_optional_number(kroko, key, f"kroko.{key}", r)
    _check_bool(kroko, "collapse_repetition_loops", "kroko.collapse_repetition_loops", r)
    _check_int_range(kroko, "repetition_loop_min_repeats", "kroko.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(kroko, "repetition_loop_max_phrase_words", "kroko.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(kroko, "repetition_loop_keep_repeats", "kroko.repetition_loop_keep_repeats", 1, 20, r)


def _check_multi_stt(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.multi_stt: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.multi_stt.enabled", r)
    _check_str(cfg, "secondary_backend", "workflow.multi_stt.secondary_backend", r)
    secondary = cfg.get("secondary_backend")
    if isinstance(secondary, str) and secondary not in {
        "cohere", "cohere_tf5", "whisper", "granite", "parakeet", "voxtral", "kroko"
    }:
        r.add_error(
            "workflow.multi_stt.secondary_backend: valeurs acceptées cohere, cohere_tf5, whisper, granite, parakeet, voxtral, kroko"
        )
    _check_int_range(cfg, "max_segments", "workflow.multi_stt.max_segments", 1, 500, r)
    for key in ("min_segment_s", "padding_s"):
        _check_optional_number(cfg, key, f"workflow.multi_stt.{key}", r)
    levels = cfg.get("levels", [])
    if not isinstance(levels, list) or not levels:
        r.add_error("workflow.multi_stt.levels: doit être une liste non vide")
    else:
        for i, value in enumerate(levels):
            if value not in {"suspect", "degrade"}:
                r.add_error(f"workflow.multi_stt.levels[{i}]: doit valoir suspect ou degrade")


def _check_stt_hybrid(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.stt_hybrid: doit être un objet YAML")
        return
    for key in ("enabled", "llm_arbitration_enabled", "write_audit_artifacts"):
        _check_bool(cfg, key, f"workflow.stt_hybrid.{key}", r)
    if cfg.get("enabled") is True:
        r.add_error("workflow.stt_hybrid.enabled: mode non encore intégré au pipeline, doit rester false")
    for key in ("primary_backend", "fallback_backend"):
        _check_str(cfg, key, f"workflow.stt_hybrid.{key}", r)
    primary = str(cfg.get("primary_backend") or "")
    fallback = str(cfg.get("fallback_backend") or "")
    if primary and fallback and primary == fallback:
        r.add_error("workflow.stt_hybrid: primary_backend et fallback_backend doivent être différents")
    for key in ("decision_margin", "window_s"):
        _check_optional_number(cfg, key, f"workflow.stt_hybrid.{key}", r)
    for key in ("fallback_on_reliability", "review_on_reliability"):
        values = cfg.get(key, [])
        if not isinstance(values, list):
            r.add_error(f"workflow.stt_hybrid.{key}: doit être une liste")
            continue
        for i, value in enumerate(values):
            if value not in {"ok", "suspect", "degrade"}:
                r.add_error(f"workflow.stt_hybrid.{key}[{i}]: doit valoir ok, suspect ou degrade")


def _check_audio_scene(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_scene: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.audio_scene.enabled", r)
    _check_bool(cfg, "detect_gender", "workflow.audio_scene.detect_gender", r)
    _check_optional_number(cfg, "timeout_s", "workflow.audio_scene.timeout_s", r)
    thresholds = cfg.get("thresholds", {})
    if thresholds:
        if not isinstance(thresholds, dict):
            r.add_error("workflow.audio_scene.thresholds: doit être un objet YAML")
        else:
            for key in (
                "energy_ratio", "min_segment_s", "noise_flatness_min",
                "music_flatness_max", "music_zcr_max", "music_suppress_bandwidth_hz",
                "female_pitch_hz", "problem_segment_min_s",
            ):
                _check_optional_number(thresholds, key, f"workflow.audio_scene.thresholds.{key}", r)


def _check_audio_scene_filter(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_scene_filter: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.audio_scene_filter.enabled", r)
    modes = cfg.get("enabled_for_modes", [])
    if not isinstance(modes, list):
        r.add_error("workflow.audio_scene_filter.enabled_for_modes: doit être une liste")
    else:
        for mode in modes:
            if mode not in {"fast", "quality"}:
                r.add_error("workflow.audio_scene_filter.enabled_for_modes: valeurs acceptées fast, quality")
    labels = cfg.get("target_labels", [])
    if not isinstance(labels, list):
        r.add_error("workflow.audio_scene_filter.target_labels: doit être une liste")
    else:
        for label in labels:
            if label not in {"music", "noise", "noEnergy"}:
                r.add_error("workflow.audio_scene_filter.target_labels: valeurs acceptées music, noise, noEnergy")
    for key in ("min_segment_s", "min_total_muted_s", "edge_keep_s", "max_intervals", "timeout_s"):
        _check_optional_number(cfg, key, f"workflow.audio_scene_filter.{key}", r)


def _check_audio_normalization(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_normalization: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.audio_normalization.enabled", r)
    _check_bool(cfg, "loudnorm_enabled", "workflow.audio_normalization.loudnorm_enabled", r)
    modes = cfg.get("enabled_for_modes", [])
    if not isinstance(modes, list):
        r.add_error("workflow.audio_normalization.enabled_for_modes: doit être une liste")
    else:
        for mode in modes:
            if mode not in {"fast", "quality"}:
                r.add_error("workflow.audio_normalization.enabled_for_modes: valeurs acceptées fast, quality")
    for key in ("target_i", "true_peak", "lra", "highpass_hz", "timeout_s", "auto_loudnorm_rms_threshold"):
        _check_optional_number(cfg, key, f"workflow.audio_normalization.{key}", r)
    weak = cfg.get("weak_voice", {})
    if weak:
        if not isinstance(weak, dict):
            r.add_error("workflow.audio_normalization.weak_voice: doit être un objet YAML")
        else:
            _check_bool(weak, "enabled", "workflow.audio_normalization.weak_voice.enabled", r)
            _check_bool(weak, "loudnorm_after_gain", "workflow.audio_normalization.weak_voice.loudnorm_after_gain", r)
            for key in ("target_rms", "max_gain", "target_i", "true_peak", "lra"):
                _check_optional_number(weak, key, f"workflow.audio_normalization.weak_voice.{key}", r)


def _check_audio_denoise(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.audio_denoise: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.audio_denoise.enabled", r)
    _check_bool(cfg, "force", "workflow.audio_denoise.force", r)
    modes = cfg.get("enabled_for_modes", [])
    if not isinstance(modes, list):
        r.add_error("workflow.audio_denoise.enabled_for_modes: doit être une liste")
    else:
        for mode in modes:
            if mode not in {"fast", "quality"}:
                r.add_error("workflow.audio_denoise.enabled_for_modes: valeurs acceptées fast, quality")
    if "trigger_flags" in cfg and not isinstance(cfg.get("trigger_flags"), list):
        r.add_error("workflow.audio_denoise.trigger_flags: doit être une liste")
    _check_str(cfg, "backend", "workflow.audio_denoise.backend", r)
    for key in ("noise_reduction_db", "noise_floor_db", "timeout_s"):
        _check_optional_number(cfg, key, f"workflow.audio_denoise.{key}", r)


def _check_source_separation(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.source_separation: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.source_separation.enabled", r)
    _check_str(cfg, "backend", "workflow.source_separation.backend", r)
    backend = cfg.get("backend")
    if isinstance(backend, str) and backend != "demucs":
        r.add_error("workflow.source_separation.backend: doit valoir demucs")
    for key in ("model", "device", "stem"):
        _check_str(cfg, key, f"workflow.source_separation.{key}", r)
    stem = cfg.get("stem")
    if isinstance(stem, str) and stem not in {"vocals", "drums", "bass", "other"}:
        r.add_error("workflow.source_separation.stem: valeurs acceptées vocals, drums, bass, other")
    _check_optional_number(cfg, "segment_s", "workflow.source_separation.segment_s", r)
    decision = cfg.get("decision", {})
    if decision:
        if not isinstance(decision, dict):
            r.add_error("workflow.source_separation.decision: doit être un objet YAML")
        else:
            for key in (
                "min_score", "min_duration_s", "scene_music_min_ratio",
                "scene_music_min_duration_s", "scene_music_min_speech_ratio_for_force",
                "scene_noise_score_ratio", "scene_noise_score",
                "scene_problem_segments_score_threshold", "scene_problem_segments_score",
            ):
                _check_optional_number(decision, key, f"workflow.source_separation.decision.{key}", r)


def _check_speaker_realignment(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.speaker_realignment: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.speaker_realignment.enabled", r)
    _check_optional_number(cfg, "min_word_overlap_s", "workflow.speaker_realignment.min_word_overlap_s", r)
    value = cfg.get("punctuation_chars")
    if value is not None and not isinstance(value, str):
        r.add_error("workflow.speaker_realignment.punctuation_chars: doit être une chaîne")


def _check_diarization(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("diarization: doit être un objet YAML")
        return
    for key in ("cache_enabled", "cache_audio_fingerprint", "embedding_cache_enabled", "preload_audio", "prepare_pcm_audio"):
        _check_bool(cfg, key, f"diarization.{key}", r)
    _check_optional_number(cfg, "embedding_clip_seconds", "diarization.embedding_clip_seconds", r)
    _check_optional_positive_int(cfg, "prepare_pcm_timeout_s", "diarization.prepare_pcm_timeout_s", r)
    _check_optional_number(cfg, "prepare_pcm_duration_tolerance_s", "diarization.prepare_pcm_duration_tolerance_s", r)
    _check_optional_positive_int(cfg, "embedding_batch_size", "diarization.embedding_batch_size", r)
    _check_optional_positive_int(cfg, "segmentation_batch_size", "diarization.segmentation_batch_size", r)
    _check_bool(cfg, "progress_log_enabled", "diarization.progress_log_enabled", r)
    _check_optional_number(cfg, "progress_log_interval_s", "diarization.progress_log_interval_s", r)
    _check_diarization_pipeline_params(cfg.get("pipeline_params"), r)


def _check_diarization_pipeline_params(cfg: object, r: ValidationResult) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        r.add_error("diarization.pipeline_params: doit être un objet YAML")
        return

    allowed = {
        "segmentation": {"min_duration_off"},
        "clustering": {"threshold", "Fa", "Fb"},
    }
    for section, values in cfg.items():
        if section not in allowed:
            r.add_error(f"diarization.pipeline_params.{section}: section non supportée")
            continue
        if values is None:
            continue
        if not isinstance(values, dict):
            r.add_error(f"diarization.pipeline_params.{section}: doit être un objet YAML")
            continue
        for key in values:
            if key not in allowed[section]:
                r.add_error(f"diarization.pipeline_params.{section}.{key}: paramètre non supporté")
        for key in allowed[section]:
            _check_optional_number(values, key, f"diarization.pipeline_params.{section}.{key}", r)


def _check_llm_section(
    llm: dict, prefix: str, r: ValidationResult, is_summary: bool = False
) -> None:
    _check_bool(llm, "enabled", f"{prefix}.enabled", r)
    # Cycle de vie (lot 2) : booléens valides même LLM désactivée (posés à l'avance).
    _check_bool(llm, "keep_warm", f"{prefix}.keep_warm", r)
    _check_bool(llm, "prelaunch_at_analyze", f"{prefix}.prelaunch_at_analyze", r)

    if not llm.get("enabled"):
        return

    _check_str(llm, "model_id", f"{prefix}.model_id", r)

    api_base = llm.get("api_base", "")
    if isinstance(api_base, str):
        if not api_base.startswith("http"):
            r.add_error(
                f"{prefix}.api_base doit commencer par http:// ou https:// "
                f"(valeur: '{api_base}')"
            )
    else:
        r.add_error(f"{prefix}.api_base doit être une chaîne de caractères")

    _check_int_range(llm, "timeout_seconds", f"{prefix}.timeout_seconds", 10, 86400, r)

    if not is_summary:
        opencode_bin = llm.get("opencode_bin", "")
        if isinstance(opencode_bin, str) and not opencode_bin.strip():
            r.add_error(f"{prefix}.opencode_bin: chemin manquant")


def _check_execution_section(exec_cfg: dict, prefix: str, r: ValidationResult) -> None:
    if exec_cfg is None:
        return
    if not isinstance(exec_cfg, dict):
        r.add_error(f"{prefix}: doit être un objet YAML")
        return
    if "max_concurrent_jobs" in exec_cfg:
        _check_int_range(exec_cfg, "max_concurrent_jobs", f"{prefix}.max_concurrent_jobs", 1, 8, r)


def _check_queue_section(queue_cfg: dict, r: ValidationResult) -> None:
    if not queue_cfg:
        return
    if not isinstance(queue_cfg, dict):
        r.add_error("workflow.queue: doit être un objet YAML")
        return
    _check_bool(queue_cfg, "enabled", "workflow.queue.enabled", r)
    _check_int_range(queue_cfg, "default_priority", "workflow.queue.default_priority", 1, 100, r)
    _check_bool(queue_cfg, "aging_enabled", "workflow.queue.aging_enabled", r)
    _check_int_range(queue_cfg, "aging_interval_minutes", "workflow.queue.aging_interval_minutes", 1, 1440, r)
    _check_int_range(queue_cfg, "aging_max_bonus", "workflow.queue.aging_max_bonus", 0, 99, r)
    _check_int_range(queue_cfg, "poll_interval_s", "workflow.queue.poll_interval_s", 1, 300, r)
    _check_int_range(queue_cfg, "starvation_timeout_hours", "workflow.queue.starvation_timeout_hours", 1, 720, r)


def _check_scheduling_section(sched_cfg: dict, r: ValidationResult) -> None:
    if not sched_cfg:
        return
    if not isinstance(sched_cfg, dict):
        r.add_error("workflow.scheduling: doit être un objet YAML")
        return
    _check_bool(sched_cfg, "enabled", "workflow.scheduling.enabled", r)
    timezone = sched_cfg.get("timezone", "Europe/Paris")
    if not isinstance(timezone, str) or not timezone.strip():
        r.add_error("workflow.scheduling.timezone: doit être une chaîne non vide")
    else:
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(timezone)
        except Exception:
            r.add_error(f"workflow.scheduling.timezone: fuseau horaire invalide '{timezone}'")
    _check_int_range(sched_cfg, "poll_interval_s", "workflow.scheduling.poll_interval_s", 10, 86400, r)
    patterns = sched_cfg.get("kill_patterns", [])
    if not isinstance(patterns, list):
        r.add_error("workflow.scheduling.kill_patterns: doit être une liste")
    else:
        for i, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern.strip():
                r.add_error(f"workflow.scheduling.kill_patterns[{i}]: chaîne vide")
    windows = sched_cfg.get("windows", [])
    if not isinstance(windows, list):
        r.add_error("workflow.scheduling.windows: doit être une liste")
        return
    valid_days = {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"}
    valid_actions = {"force_gpu", "pause_queue", "limit_concurrency", "none"}
    for i, window in enumerate(windows):
        if not isinstance(window, dict):
            r.add_error(f"workflow.scheduling.windows[{i}]: doit être un objet YAML")
            continue
        _check_str(window, "name", f"workflow.scheduling.windows[{i}].name", r)
        _check_time_string(window, "start", f"workflow.scheduling.windows[{i}].start", r)
        _check_time_string(window, "end", f"workflow.scheduling.windows[{i}].end", r)
        action = window.get("action")
        if action not in valid_actions:
            r.add_error(f"workflow.scheduling.windows[{i}].action: valeur invalide '{action}'")
        days = window.get("days", [])
        if not isinstance(days, list) or not days:
            r.add_error(f"workflow.scheduling.windows[{i}].days: doit être une liste non vide")
        else:
            for day in days:
                if day not in valid_days:
                    r.add_error(f"workflow.scheduling.windows[{i}].days: jour invalide '{day}'")
        _check_bool(window, "enabled", f"workflow.scheduling.windows[{i}].enabled", r)


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


def _check_str(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        r.add_error(f"{path}: valeur manquante")
    elif not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne (reçu {type(val).__name__})")
    elif val.strip() == "":
        r.add_error(f"{path}: chaîne vide")


def _check_bool(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is not None and not isinstance(val, bool):
        r.add_error(f"{path}: doit être true/false (reçu {type(val).__name__})")


def _check_time_string(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne HH:MM")
        return
    if not re.match(r"^\d{2}:\d{2}$", val):
        r.add_error(f"{path}: doit être au format HH:MM")
        return
    hour, minute = [int(part) for part in val.split(":")]
    if hour > 23 or minute > 59:
        r.add_error(f"{path}: heure invalide")


def _check_int_range(
    obj: dict, key: str, path: str, vmin: int, vmax: int, r: ValidationResult
) -> None:
    val = obj.get(key)
    if val is None:
        r.add_error(f"{path}: valeur manquante")
        return
    if isinstance(val, bool):
        r.add_error(f"{path}: doit être un nombre entier, pas un booléen")
        return
    if not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un nombre (reçu {type(val).__name__})")
        return
    val = int(val)
    if val < vmin or val > vmax:
        r.add_error(f"{path}={val}: doit être entre {vmin} et {vmax}")


def _check_optional_number(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un nombre ou null")


def _check_optional_positive_int(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, int):
        r.add_error(f"{path}: doit être un entier positif ou null")
        return
    if val < 1:
        r.add_error(f"{path}: doit être >= 1 ou null")


def _check_regex_string(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    val = obj.get(key)
    if val is None:
        return
    if not isinstance(val, str):
        r.add_error(f"{path}: doit être une chaîne ou null")
        return
    if not val.strip():
        return
    try:
        re.compile(val)
    except re.error as exc:
        r.add_error(f"{path}: regex invalide ({exc})")


def _check_regex_list(obj: dict, key: str, path: str, r: ValidationResult) -> None:
    values = obj.get(key, [])
    if values is None:
        return
    if not isinstance(values, list):
        r.add_error(f"{path}: doit être une liste")
        return
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, str) or not value.strip():
            r.add_error(f"{item_path}: doit être une chaîne non vide")
            continue
        try:
            re.compile(value)
        except re.error as exc:
            r.add_error(f"{item_path}: regex invalide ({exc})")


def _check_port_value(val: Any, path: str, r: ValidationResult) -> None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un port numérique")
        return
    port = int(val)
    if port < 1 or port > 65535:
        r.add_error(f"{path}={port}: doit être entre 1 et 65535")

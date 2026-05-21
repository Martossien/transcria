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
    _check_auth(cfg.get("auth", {}), result)
    _check_gpu(cfg.get("gpu", {}), result)
    _check_services(cfg.get("services", {}), result)
    _check_models(cfg.get("models", {}), result)
    _check_whisper(cfg.get("whisper", {}), result)
    _check_workflow(cfg.get("workflow", {}), result)
    _check_diarization(cfg.get("diarization", {}), result)
    _check_quality(cfg.get("quality", {}), result)
    _check_security(cfg.get("security", {}), result)
    return result


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
    _check_int_range(gpu, "min_free_vram_mb", "gpu.min_free_vram_mb", 100, 50000, r)


def _check_services(svc: dict, r: ValidationResult) -> None:
    _check_str(svc, "dashboard_llm_url", "services.dashboard_llm_url", r)
    _check_str(svc, "srt_editor_easy_url", "services.srt_editor_easy_url", r)
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


def _check_models(mod: dict, r: ValidationResult) -> None:
    _check_stt_backend(mod, r)
    _check_str(mod, "default_stt_model", "models.default_stt_model", r)
    _check_str(mod, "fallback_stt_model", "models.fallback_stt_model", r)
    _check_str(mod, "cohere_model_path", "models.cohere_model_path", r)
    _check_str(mod, "pyannote_model", "models.pyannote_model", r)

    stt_model = mod.get("stt_backend", "")
    cohere_path = mod.get("cohere_model_path", "")
    if isinstance(stt_model, str) and stt_model == "cohere" and not cohere_path:
        r.add_error(
            "models.cohere_model_path doit être renseigné quand le backend STT est 'cohere'"
        )


def _check_stt_backend(mod: dict, r: ValidationResult) -> None:
    valid = {"cohere", "whisper"}
    backend = mod.get("stt_backend", "cohere")
    if not isinstance(backend, str) or backend not in valid:
        r.add_error(
            f"models.stt_backend='{backend}' invalide. "
            f"Valeurs acceptées: {', '.join(sorted(valid))}"
        )


def _check_workflow(wf: dict, r: ValidationResult) -> None:
    _check_bool(wf, "enable_quick_summary", "workflow.enable_quick_summary", r)
    _check_bool(wf, "enable_speaker_detection", "workflow.enable_speaker_detection", r)
    _check_bool(wf, "enable_quality_mode", "workflow.enable_quality_mode", r)
    _check_bool(wf, "enable_external_srt_editor_link", "workflow.enable_external_srt_editor_link", r)
    _check_execution_section(wf.get("execution", {}), "workflow.execution", r)
    _check_audio_quality(wf.get("audio_quality", {}), r)
    _check_quality_transcription(wf.get("quality_transcription", {}), r)
    _check_vad_section(wf.get("vad", {}), r)
    _check_audio_scene_filter(wf.get("audio_scene_filter", {}), r)
    _check_speaker_realignment(wf.get("speaker_realignment", {}), r)

    _check_llm_section(wf.get("summary_llm", {}), "workflow.summary_llm", r, is_summary=True)
    _check_llm_section(wf.get("arbitration_llm", {}), "workflow.arbitration_llm", r, is_summary=False)


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


def _check_quality_transcription(cfg: dict, r: ValidationResult) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.quality_transcription: doit être un objet YAML")
        return
    backend = cfg.get("force_stt_backend")
    if backend is not None and backend not in {"cohere", "whisper"}:
        r.add_error("workflow.quality_transcription.force_stt_backend: doit valoir cohere ou whisper")
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


def _check_vad_section(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.vad: doit être un objet YAML")
        return
    for key in ("enabled_summary", "enabled_final", "adaptive"):
        _check_bool(cfg, key, f"workflow.vad.{key}", r)
    for key in (
        "threshold", "threshold_low_quality", "threshold_high_noise",
        "min_speech_duration_ms", "min_silence_duration_ms",
        "min_silence_duration_ms_low_quality", "speech_pad_ms",
        "speech_pad_ms_low_quality",
    ):
        _check_optional_number(cfg, key, f"workflow.vad.{key}", r)


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
    for key in ("cache_enabled", "cache_audio_fingerprint", "embedding_cache_enabled"):
        _check_bool(cfg, key, f"diarization.{key}", r)
    _check_optional_number(cfg, "embedding_clip_seconds", "diarization.embedding_clip_seconds", r)


def _check_llm_section(
    llm: dict, prefix: str, r: ValidationResult, is_summary: bool = False
) -> None:
    _check_bool(llm, "enabled", f"{prefix}.enabled", r)

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


def _check_quality(quality: dict, r: ValidationResult) -> None:
    markers = quality.get("asr_noise_markers", [])
    if markers is None:
        return
    if not isinstance(markers, list):
        r.add_error("quality.asr_noise_markers: doit être une liste")
        return
    for i, marker in enumerate(markers):
        if not isinstance(marker, str) or not marker.strip():
            r.add_error(f"quality.asr_noise_markers[{i}]: doit être une chaîne non vide")


def _check_security(sec: dict, r: ValidationResult) -> None:
    _check_int_range(sec, "retention_days", "security.retention_days", 0, 3650, r)
    _check_bool(sec, "allow_job_delete", "security.allow_job_delete", r)

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


def _check_port_value(val: Any, path: str, r: ValidationResult) -> None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        r.add_error(f"{path}: doit être un port numérique")
        return
    port = int(val)
    if port < 1 or port > 65535:
        r.add_error(f"{path}={port}: doit être entre 1 et 65535")


def sanitize_llm_config(wf: dict) -> dict:
    wf = dict(wf)
    for section in ("summary_llm", "arbitration_llm"):
        if section in wf and isinstance(wf[section], dict):
            if not wf[section].get("enabled", True):
                wf[section] = {"enabled": False}
    return wf

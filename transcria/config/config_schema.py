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
    _check_workflow(cfg.get("workflow", {}), result)
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
    _check_int_range(svc, "qwen_port", "services.qwen_port", 1, 65535, r)
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

    _check_llm_section(wf.get("summary_llm", {}), "workflow.summary_llm", r, is_summary=True)
    _check_llm_section(wf.get("arbitration_llm", {}), "workflow.arbitration_llm", r, is_summary=False)


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


def sanitize_llm_config(wf: dict) -> dict:
    wf = dict(wf)
    for section in ("summary_llm", "arbitration_llm"):
        if section in wf and isinstance(wf[section], dict):
            if not wf[section].get("enabled", True):
                wf[section] = {"enabled": False}
    return wf

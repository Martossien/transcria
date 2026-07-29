"""Schéma de config — orchestration : workflow (hub des sections) + LLM/exécution/file/planification."""
from __future__ import annotations

from transcria.config.checks.audio import (  # noqa: F401
    _check_audio_denoise,
    _check_audio_normalization,
    _check_audio_preflight,
    _check_audio_quality,
    _check_audio_scene,
    _check_audio_scene_filter,
    _check_pyannote_chunking,
    _check_segment_reliability,
    _check_source_separation,
    _check_speaker_realignment,
    _check_transcription_cleanup,
    _check_vad_section,
)
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
from transcria.config.checks.stt import (  # noqa: F401
    _check_multi_stt,
    _check_quality_transcription,
    _check_stt_hybrid,
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

"""Schéma de config — chaîne audio : qualité, VAD, nettoyage, scène, séparation, diarisation."""
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

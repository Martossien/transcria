"""Validation du schéma de config — FAÇADE + dispatcher.

Depuis la vague 0 de consolidation (2026-07), les vérificateurs vivent dans
``transcria/config/checks/`` par domaine : ``base`` (ValidationResult + primitives typées),
``platform``, ``auth``, ``stt``, ``audio``, ``orchestration`` (le hub ``_check_workflow``).
Ce module reste le POINT D'ENTRÉE unique : il porte ``validate_config`` (QUELLES sections
sont validées — l'ordre des appels est le contrat) et ré-exporte toute la surface
historique. Golden : ``tests/test_config_schema_golden.py`` (messages exacts figés sur
configs types + « les défauts du loader valident sans erreur »).
"""
from __future__ import annotations

from transcria.config.checks.audio import (  # noqa: F401 — façade
    _check_audio_denoise,
    _check_audio_normalization,
    _check_audio_preflight,
    _check_audio_quality,
    _check_audio_scene,
    _check_audio_scene_filter,
    _check_diarization,
    _check_diarization_pipeline_params,
    _check_pyannote_chunking,
    _check_segment_reliability,
    _check_source_separation,
    _check_speaker_realignment,
    _check_transcription_cleanup,
    _check_vad_section,
)
from transcria.config.checks.auth import (  # noqa: F401 — façade
    _IMPLEMENTED_AUTH_BACKENDS,
    _check_auth,
    _check_auth_backend,
    _check_auth_ldap,
    _check_auth_oidc,
    _check_auth_proxy,
    _check_role_mapping_federated,
)
from transcria.config.checks.base import (  # noqa: F401 — façade
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
from transcria.config.checks.orchestration import (  # noqa: F401 — façade
    _check_execution_section,
    _check_llm_section,
    _check_progress_section,
    _check_queue_section,
    _check_scheduling_section,
    _check_workflow,
)
from transcria.config.checks.platform import (  # noqa: F401 — façade
    _KNOWN_LOCALES,
    _check_gpu,
    _check_i18n,
    _check_live,
    _check_maintenance,
    _check_quality,
    _check_required_keys,
    _check_security,
    _check_server,
    _check_services,
    _check_storage,
    _check_voice_enrollment,
)
from transcria.config.checks.stt import (  # noqa: F401 — façade
    _VALID_STT_BACKENDS,
    _check_cohere,
    _check_cohere_tf5,
    _check_granite,
    _check_kroko,
    _check_live_stt_backend,
    _check_models,
    _check_moss,
    _check_multi_stt,
    _check_quality_transcription,
    _check_stt_backend,
    _check_stt_hybrid,
    _check_stt_served_pools,
    _check_summary_stt_backend,
    _check_voxtral,
    _check_whisper,
)


def validate_config(cfg: dict) -> ValidationResult:
    result = ValidationResult()
    _check_required_keys(cfg, result)
    _check_server(cfg.get("server", {}), result)
    _check_storage(cfg.get("storage", {}), result)
    _check_voice_enrollment(cfg.get("voice_enrollment", {}), result)
    _check_auth(cfg.get("auth", {}), result)
    _check_auth_backend(cfg.get("auth", {}) or {}, result)
    _check_auth_oidc(cfg.get("auth", {}) or {}, result)
    _check_auth_proxy(cfg.get("auth", {}) or {}, result)
    _check_auth_ldap(cfg.get("auth", {}) or {}, result)
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
    _check_live(cfg.get("live", {}), result)
    return result

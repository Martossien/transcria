"""Goldens préalables à la vague B2 (moteur d'étapes du pipeline).

Fige, octet pour octet, la séquence d'étapes générée par
``_define_pipeline_steps_for_profile`` pour CHAQUE profil (y compris legacy)
sous les variantes de flags qui gatent la table (multi-STT, LLM d'arbitrage,
micro-étape « champs du type »). Chaque étape est sérialisée avec l'identité
de la méthode liée du runner et le rôle de ses arguments.

Un échec ici pendant la vague B2 signifie que l'extraction a CHANGÉ le
comportement — c'est un signal d'arrêt, pas un golden à régénérer.
"""
# ruff: noqa: E501 — les séquences golden sont des littéraux d'une ligne, à dessein.
import pytest

from transcria.services.pipeline_service import PipelineService
from transcria.workflow import profiles
from transcria.workflow.runner import WorkflowRunner

GOLDEN_SEQUENCES = {
    ("base", "legacy_fast"): "correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "srt_express"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "srt_locuteurs"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "srt_moss"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "word_rapide"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "word_structure"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "word_corrige"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("base", "dossier_qualite"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "legacy_fast"): "multi_stt_review=WorkflowRunner.run_multi_stt_review(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "srt_express"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "srt_locuteurs"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "srt_moss"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "word_rapide"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "word_structure"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "word_corrige"): "multi_stt_review=WorkflowRunner.run_multi_stt_review(job,audio,config) | diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("multi_stt", "dossier_qualite"): "multi_stt_review=WorkflowRunner.run_multi_stt_review(job,audio,config) | diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "legacy_fast"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "srt_express"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "srt_locuteurs"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "srt_moss"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "word_rapide"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "word_structure"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "word_corrige"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("llm_off", "dossier_qualite"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "legacy_fast"): "correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "srt_express"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "srt_locuteurs"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "srt_moss"): "quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "word_rapide"): "type_fields=WorkflowRunner.run_type_field_extraction(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "word_structure"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | type_fields=WorkflowRunner.run_type_field_extraction(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "word_corrige"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
    ("type_fields", "dossier_qualite"): "diarization=WorkflowRunner.run_diarization(job,audio,config) | correction=WorkflowRunner.run_correction(job,config) | final_review=WorkflowRunner.run_final_review(job,config) | quality=WorkflowRunner.run_quality_checks(job,config) | export=WorkflowRunner.build_export(job,config)",
}

VARIANT_CONFIGS = {
    "base": {"workflow": {"enable_quality_mode": True}},
    "multi_stt": {"workflow": {"enable_quality_mode": True, "multi_stt": {"enabled": True}}},
    "llm_off": {"workflow": {"enable_quality_mode": True, "arbitration_llm": {"enabled": False}}},
    "type_fields": {"workflow": {"enable_quality_mode": True}},
}


class _Job:
    id = "golden-job"


def _serialize(svc: PipelineService, job, audio: str, profile) -> str:
    parts = []
    for step in svc._define_pipeline_steps_for_profile(job, audio, profile):
        method = step["method"]
        roles = ",".join(
            "job" if arg is job else "audio" if arg is audio else "config" if arg is svc.config else "?"
            for arg in method.args
        )
        parts.append(f"{step['name']}={method.func.__qualname__}({roles})")
    return " | ".join(parts)


@pytest.mark.parametrize("variant,profile_id", sorted(GOLDEN_SEQUENCES))
def test_step_sequence_frozen(variant, profile_id):
    svc = PipelineService.__new__(PipelineService)
    svc.config = VARIANT_CONFIGS[variant]
    svc.runner = WorkflowRunner.__new__(WorkflowRunner)  # méthodes liées réelles, sans infra
    if variant == "type_fields":
        svc._job_has_type_extract_fields = lambda job: True

    job, audio = _Job(), "/tmp/golden.wav"
    actual = _serialize(svc, job, audio, profiles.get_profile(profile_id))
    assert actual == GOLDEN_SEQUENCES[(variant, profile_id)]


def test_every_profile_is_goldened():
    covered = {profile_id for _, profile_id in GOLDEN_SEQUENCES}
    assert covered == {p.id for p in profiles.list_profiles(include_legacy=True)}

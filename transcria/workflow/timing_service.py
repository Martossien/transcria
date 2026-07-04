"""Façade du modèle de temps : profil → étapes → estimation calibrée machine.

Source UNIQUE consommée par le wizard (estimation avant lancement), l'email « résumé
prêt » (temps de traitement restant), la page de suivi (ETA live) et la file d'attente.
Relie `profiles` (quelles étapes un profil exécute), `JobTimingStore` (historique réel)
et `timing_model` (logique pure). Voir [[timing_model]].
"""
from __future__ import annotations

from transcria.workflow.timing_model import (
    Estimate,
    estimate_machine,
    human_review_minutes,
    legacy_machine_seconds,
)

# Étape enregistrée pour la phase RÉSUMÉ (STT rapide + diarisation + LLM résumé).
SUMMARY_STAGE = "summary"


def processing_stages(profile) -> list[str]:
    """Étapes du TRAITEMENT qu'un profil exécute, dans les noms RÉELLEMENT historisés
    (`transcribe`, `diarization`, `correction`, `final_review`, `quality`, `export`)."""
    stages: list[str] = []
    if profile.resource_requirements.needs_stt:
        stages.append("transcribe")
    if profile.run_diarization:
        stages.append("diarization")
    if profile.run_llm_correction:
        stages.append("correction")
    if profile.run_final_review:
        stages.append("final_review")
    if profile.run_quality != "none":
        stages.append("quality")
    if profile.docx_level != "none" or profile.zip_level != "none":
        stages.append("export")
    return stages


def _estimate_stages(profile_id: str, stages: list[str], audio_seconds: float) -> Estimate:
    from transcria.jobs.timing_store import JobTimingStore

    samples = JobTimingStore.samples_for_stages(profile_id, stages)
    return estimate_machine(stages, samples, audio_seconds)


def estimate_processing(profile, audio_seconds: float) -> Estimate:
    """Temps machine du TRAITEMENT (post-résumé) — calibré si historique, sinon formule."""
    return _estimate_stages(profile.id, processing_stages(profile), audio_seconds)


def estimate_summary(profile, audio_seconds: float) -> Estimate:
    """Temps machine de la phase RÉSUMÉ (STT+diarisation+LLM)."""
    return _estimate_stages(profile.id, [SUMMARY_STAGE], audio_seconds)


def estimate_total_machine(profile, audio_seconds: float) -> Estimate:
    """Temps machine TOTAL (résumé + traitement) — pour l'estimation du wizard.

    Combine les deux phases ; si l'une retombe sur la formule, le total reste cohérent
    (les fourchettes s'additionnent). Un profil sans résumé n'ajoute que le traitement.
    """
    summ = estimate_summary(profile, audio_seconds) if profile.requires_summary else Estimate(0, "measured", 0, 0, 0)
    proc = estimate_processing(profile, audio_seconds)
    basis = "measured" if summ.basis == "measured" and proc.basis == "measured" else "initial"
    if basis == "initial":
        est = legacy_machine_seconds(audio_seconds)
        return Estimate(est, "initial", est * 0.75, est * 1.25, 0)
    return Estimate(
        summ.seconds + proc.seconds, "measured",
        summ.low_seconds + proc.low_seconds, summ.high_seconds + proc.high_seconds,
        min(summ.samples, proc.samples),
    )


def estimate_total_with_human(profile, audio_seconds: float) -> dict:
    """Estimation affichée au wizard : machine (calibré) + validation humaine.

    Renvoie un dict prêt pour le template : minutes totales, fourchette lisible, base de
    confiance (« measured »/« initial ») — pour un affichage honnête.
    """
    from transcria.workflow.timing_model import format_range_fr

    machine = estimate_total_machine(profile, audio_seconds)
    human_min = human_review_minutes(audio_seconds)
    total_min = round(machine.seconds / 60 + human_min, 1) if audio_seconds > 0 else None
    return {
        "total_minutes": total_min,
        "machine_range": format_range_fr(machine),
        "human_minutes": human_min,
        "basis": machine.basis,
        "samples": machine.samples,
    }

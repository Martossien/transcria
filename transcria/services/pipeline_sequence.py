"""Séquencement des étapes du pipeline (vague B2, lot 2).

Corps extraits de ``PipelineService`` : ``define_pipeline_steps_for_profile``
reste **l'unique table de séquencement** (garantie par les goldens B2 — la
séquence par profil est identique octet pour octet). Les fonctions prennent le
service en premier argument et rappellent ses coutures (``svc.runner``,
``svc._job_has_type_extract_fields``) : les tests substituent ces méthodes à
l'instance.
"""
from functools import partial

from transcria.config.views import WorkflowView
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.workflow import profiles
from transcria.workflow.type_field_extraction import extract_fields_from_type


def resolve_profile(job: Job, mode: str):
    """Profil de traitement effectif du job.

    Priorité au profil persisté à l'enfilage (`extra_data.execution.processing_profile_id`,
    cf. Phase 2) ; à défaut, dérivé du `mode` legacy (fast/quality). Repli ultime sur le
    profil de `fast` pour un mode/étape inconnu (jamais d'exception ici).
    """

    try:
        pid = (job.get_extra_data().get("execution", {}) or {}).get("processing_profile_id")
    except Exception:  # noqa: BLE001 — job non-DB en test : on retombe sur le mode
        pid = None
    if pid and profiles.is_profile(pid):
        return profiles.get_profile(pid)
    try:
        return profiles.get_profile(profiles.resolve_legacy_mode(mode))
    except (KeyError, ValueError):
        return profiles.get_profile(profiles.resolve_legacy_mode("fast"))


def job_has_type_extract_fields(config: dict, job) -> bool:
    """Le job a-t-il un type de réunion perso matérialisé AVEC des extract_fields ?
    (garde de la micro-étape « champs du type » — évite tout coût quand inutile)."""
    try:
        fs = JobFilesystem(config["storage"]["jobs_dir"], job.id)
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        custom_type = meeting_ctx.get("custom_type")
        return bool(extract_fields_from_type(custom_type if isinstance(custom_type, dict) else None))
    except Exception:  # noqa: BLE001 — une garde ne doit jamais casser la construction du pipeline
        return False


def define_pipeline_steps_for_profile(svc, job: Job, audio_path: str, profile) -> list[dict]:
    """Étapes machine du pipeline À PARTIR DU PROFIL (gating par flags du profil).

    Parité stricte avec l'ancien gating mode-based (golden) : la diarisation reste aussi
    conditionnée à `enable_quality_mode`, la correction au flag global `arbitration_llm.enabled`.
    Ainsi `dossier_qualite` reproduit l'ancien `quality` et `legacy_fast` l'ancien `fast`.
    """
    wf = WorkflowView.from_config(svc.config)
    steps = []

    # Multi-STT EXPÉRIMENTAL : retranscription des segments dégradés par un 2e
    # moteur + arbitrage LLM. Juste après la transcription, avant tout consommateur
    # du SRT. Gated : flag config explicite ET profil avec correction LLM (les
    # profils express ne touchent jamais à une LLM). Best-effort dans le runner.
    if (
        wf.multi_stt_enabled
        and profile.run_llm_correction
        and wf.arbitration_llm_enabled
    ):
        steps.append({
            "name": "multi_stt_review",
            "method": partial(svc.runner.run_multi_stt_review, job, audio_path, svc.config),
        })

    if profile.run_diarization and wf.enable_quality_mode:
        steps.append({
            "name": "diarization",
            "method": partial(svc.runner.run_diarization, job, audio_path, svc.config),
        })

    if profile.run_llm_correction and wf.arbitration_llm_enabled:
        steps.append({
            "name": "correction",
            "method": partial(svc.runner.run_correction, job, svc.config),
        })
        # Relecture finale (A+C+D+G) : harmonisation synthèse, cohérence/variantes
        # du SRT corrigé, audit des données structurées. Après correction (besoin
        # du SRT corrigé complet) et avant la qualité (pour que le score reflète le
        # SRT relu). Best-effort : n'interrompt pas le pipeline.
        if profile.run_final_review:
            steps.append({
                "name": "final_review",
                "method": partial(svc.runner.run_final_review, job, svc.config),
            })
    # Micro-étape « champs du type » : SEULEMENT si le profil ne fait PAS de relecture
    # finale (qui les extrait déjà) ET qu'un type perso avec extract_fields est choisi
    # (trou macro Word structuré). Coût GPU nul sinon — cf. type_field_extraction.
    if (not profile.run_final_review and profile.requires_summary
            and svc._job_has_type_extract_fields(job)):
        steps.append({
            "name": "type_fields",
            "method": partial(svc.runner.run_type_field_extraction, job, svc.config),
        })
    if profile.run_quality != "none":
        steps.append({
            "name": "quality",
            "method": partial(svc.runner.run_quality_checks, job, svc.config),
        })
    if profile.docx_level != "none" or profile.zip_level != "none":
        steps.append({
            "name": "export",
            "method": partial(svc.runner.build_export, job, svc.config),
        })

    return steps


def define_pipeline_steps(svc, job: Job, audio_path: str, mode: str) -> list[dict]:
    """Entrée legacy mode-based : résout le profil et délègue (source unique du gating)."""
    return svc._define_pipeline_steps_for_profile(job, audio_path, resolve_profile(job, mode))

"""Estimation VRAM d'admission d'un traitement (vague B2, lot 2).

Corps extraits de ``PipelineService.estimate_profile_resources`` /
``estimate_job_vram``. Fonctions pures de (config, profil) — consommées par
l'admission (`QueueScheduler`) et les routes wizard via les délégateurs
statiques du service.
"""
from transcria.config.views import GpuView, SttView, WorkflowView


def estimate_profile_resources(config: dict, profile) -> dict:
    """Profil VRAM d'admission, dérivé des phases RÉELLES du profil de traitement.

    Ne réserve que ce que le profil exécute : un profil sans LLM n'expose pas de phase
    `llm_arbitration` (donc l'admission ne le bloque jamais derrière la LLM — cf.
    `QueueScheduler._llm_admissible`), un profil sans diarisation pas de phase
    `diarization`. C'est le mécanisme qui garantit « les profils légers ne sont pas
    bloqués par les ressources qu'ils n'utilisent pas » sans toucher au scheduler.

    `profile` : un `transcria.workflow.profiles.ProcessingProfile`.
    """
    from transcria.stt.diarizer_factory import get_diarizer_vram_mb
    from transcria.stt.transcriber_factory import get_backend_vram_mb
    from transcria.workflow.profiles import profile_to_legacy_mode

    stt = SttView.from_config(config)
    rr = profile.resource_requirements
    phases: dict[str, int] = {}
    if rr.needs_stt:
        phases["stt"] = get_backend_vram_mb(stt.stt_backend, config)
    if rr.needs_diarization:
        phases["diarization"] = get_diarizer_vram_mb(stt.diarization_backend, config)
    # La LLM (résumé/correction) partage le même serveur d'arbitrage : on conditionne sa
    # réservation au flag global `arbitration_llm.enabled` (comme l'estimateur historique),
    # en plus du besoin du profil.
    if rr.needs_llm and WorkflowView.from_config(config).arbitration_llm_enabled:
        phases["llm_arbitration"] = GpuView.from_config(config).llm_vram_mb
    return {
        "mode": profile_to_legacy_mode(profile),
        "processing_profile_id": profile.id,
        "peak_vram_mb": max(phases.values()) if phases else 0,
        "phases": phases,
        # HÉRITÉ (affichage seulement) : l'admission n'utilise PLUS ce drapeau — elle
        # interroge la vérité vivante (LLM en marche → partagée ; éteinte → can_host_llm
        # multi-GPU). Cf. QueueScheduler._llm_admissible et l'audit VRAM du 11/06/2026.
        "llm_shared": "llm_arbitration" in phases,
    }


def estimate_job_vram(config: dict, mode: str) -> dict:
    """Estimateur historique mode-based — délègue à `estimate_profile_resources`.

    Conservé pour les appelants qui ne disposent que d'un `mode` legacy (`fast`/`quality`)
    ou d'un id de profil. Source unique : la sélection des phases vit dans
    `estimate_profile_resources`. Pour `fast`/`quality` (seuls modes atteignant cet
    estimateur via les routes), le résultat est identique au comportement antérieur.
    """
    from transcria.workflow.profiles import get_profile, resolve_legacy_mode

    return estimate_profile_resources(config, get_profile(resolve_legacy_mode(mode)))

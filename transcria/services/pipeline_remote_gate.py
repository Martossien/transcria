"""Pré-vol des ressources distantes du pipeline (vague B2, lot 2).

Corps extrait de ``PipelineService._remote_resource_gate`` (admission §7.2 +
auto-lancement STT). L'import de ``prepare_remote_resources`` reste différé
DANS la fonction : les tests substituent le pré-vol à sa source
(``transcria.inference.resource_gate``).
"""
from transcria.jobs.models import Job
from transcria.workflow.outcomes import OutcomeKind, PhaseOutcome


def remote_resource_gate(config: dict, job: Job, sl) -> dict | None:
    """Retourne None si on peut poursuivre ; sinon un dict d'erreur (le job sera
    marqué FAILED par l'appelant). Aucun coût en mode tout-local (sortie immédiate
    du gate). Voir docs/SERVICE_RESSOURCES_GPU.md §7.
    """
    from transcria.inference.resource_gate import prepare_remote_resources
    from transcria.inference.resource_status import remote_requirements

    # Tout-local : aucun pré-vol, aucun effet de bord (cas le plus courant).
    if not remote_requirements(config):
        return None

    try:
        since = job.get_extra_data().get("_remote_unavailable_since")
    except Exception:  # noqa: BLE001
        since = None

    verdict = prepare_remote_resources(config, unavailable_since=since)

    # Suivi de la durée d'indisponibilité (best-effort : nécessite un contexte DB).
    try:
        from transcria.jobs.store import JobStore

        JobStore.update_extra_data(
            job.id, lambda d: {**d, "_remote_unavailable_since": verdict.unavailable_since}
        )
    except Exception:  # noqa: BLE001 — hors app context (tests) : non bloquant
        pass

    if verdict.action == "proceed":
        return None
    if verdict.action == "fail":
        sl.warning("Pré-vol ressources : ÉCHEC — %s", verdict.reason, job_id=job.id)
        return PhaseOutcome(
            OutcomeKind.FAILED,
            phase="preflight",
            reason=f"ressources_distantes_indisponibles: {verdict.reason}",
        ).to_legacy_dict()
    # defer (transitoire) — re-queue différé (§7.2) : le job patiente puis re-tente.
    sl.warning("Pré-vol ressources : indisponibles (transitoire) — mise en file différée (%s)",
               verdict.reason, job_id=job.id)
    return PhaseOutcome(
        OutcomeKind.DEFERRED,
        phase="preflight",
        reason=verdict.reason,
        retry_after_s=verdict.retry_after_s,
    ).to_legacy_dict()

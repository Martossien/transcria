"""Étapes audio du pipeline — une étape = un module (vague B2, lot 1).

Convention (héritée de la vague B1) : chaque module expose des fonctions qui
reçoivent le ``PipelineService`` (hôte) en premier argument et rappellent ses
coutures (``svc.config``, ``svc.progress``) — les tests substituent les
méthodes ``_run_audio_*`` à l'instance du service, qui restent le point de
passage unique (délégateurs une ligne). Le contrat typé des étapes (Protocol,
sortie ``PhaseOutcome``) arrive au lot 2 avec la boucle moteur.
"""
from transcria.jobs.filesystem import JobFilesystem


def job_fs(config: dict, job_id: str) -> JobFilesystem:
    """L'unique lecture du chemin des jobs pour les étapes audio."""
    return JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job_id)

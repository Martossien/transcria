"""Reprise du pipeline — marqueurs de phases et provenance (vague B2, lot 2).

Sort les fermetures ``_checkpoint``/``_done`` de
``PipelineService._run_pipeline_steps``. Une phase n'est sautée que si
marqueur + artefact + PROVENANCE intacte (empreintes sha256 de ses entrées,
prises au checkpoint). Quand une phase amont se rejoue, les empreintes des
phases aval ne correspondent plus → elles se ré-exécutent (jamais de
rapport/export calculé sur du périmé). Voir docs/PIPELINE_REPRISE.md.
"""
from transcria.jobs import artifact_store
from transcria.jobs.models import Job
from transcria.workflow import resume


class CheckpointManager:
    """Marqueurs + empreintes d'un dispatch : chargés une fois (état du
    dispatch courant), tenus à jour en mémoire ET en base à chaque transition."""

    def __init__(self, store, config: dict, job: Job, fs, sl) -> None:
        self.store = store
        self.config = config
        self.job_id = job.id
        self.fs = fs
        self.sl = sl
        self.done = set(resume.get_completed_phases(job))
        self.recorded_fps = resume.get_phase_fingerprints(job)

    def checkpoint(self, phase: str) -> None:
        # Empreintes AVANT le push : la provenance décrit les fichiers locaux qui
        # viennent de servir/d'être produits. Backend `pg` (split) : les artefacts
        # doivent être DURABLES en base avant le marqueur — sinon un autre tier
        # croirait la phase faite sans ses fichiers. Si le push échoue, la phase
        # n'est pas marquée → rejouée au prochain dispatch.
        fingerprints = resume.compute_input_fingerprints(phase, self.fs)
        artifact_store.push_job_files(self.config, self.job_id)
        resume.mark_phase_done(self.store, self.job_id, phase, fingerprints)
        self.done.add(phase)
        self.recorded_fps[phase] = fingerprints

    def is_done(self, phase: str) -> bool:
        if phase in self.done:
            if resume.phase_state_valid(phase, self.fs, self.recorded_fps.get(phase)):
                return True
            # Provenance invalide (une phase amont s'est rejouée, artefact manquant,
            # ou marqueur legacy sans empreintes) : on retire le marqueur EN BASE
            # avant d'exécuter — l'admission VRAM et l'UI restent vraies même si un
            # vram_wait coupe la chaîne ici. Doute → re-run, jamais de skip périmé.
            self.sl.warning("Étape invalidée — entrées modifiées en amont, ré-exécution", step=phase)
            resume.unmark_phase(self.store, self.job_id, phase)
            self.done.discard(phase)
            self.recorded_fps.pop(phase, None)
            return False
        if phase == "transcription" and resume.artifact_exists(phase, self.fs):
            # Rétro-remplissage limité à la phase la plus chère, sans entrée
            # empreintée : SRT présent ⇒ STT fait (run interrompu avant le marqueur).
            self.checkpoint(phase)
            return True
        return False

    def mark_skipped(self, phase: str, reason: str) -> None:
        resume.mark_phase_skipped(self.store, self.job_id, phase, reason)

"""Porte unique des transitions d'état — ce qui est possible, et ce qui ne l'est pas.

`Job.state` est une chaîne, et `JobStore.update_state` acceptait n'importe quelle valeur
depuis n'importe quel état, pour quarante et un appelants. Un job archivé pouvait donc
« repartir » en transcription par simple effet de bord.

Ce filet est VOLONTAIREMENT étroit : il n'interdit que l'absurde. Une matrice bavarde serait
contournée par un `force=True` généralisé, et ne protégerait plus rien.
"""
from __future__ import annotations

import uuid

import pytest

from transcria.jobs.models import JobState
from transcria.jobs.transitions import (
    FROM_TERMINAL,
    TERMINAL,
    InvalidTransition,
    ensure_allowed,
    refusal_reason,
)


class TestCeQuiPasse:
    @pytest.mark.parametrize("depuis,vers", [
        (JobState.CREATED, JobState.UPLOADED),
        (JobState.ANALYZED, JobState.READY_TO_PROCESS),
        (JobState.TRANSCRIBING, JobState.DIARIZING),
        (JobState.DIARIZING, JobState.FAILED),
        (JobState.FAILED, JobState.READY_TO_PROCESS),      # relance d'un échec : chemin COURANT
        (JobState.EXPORT_READY, JobState.COMPLETED),
    ])
    def test_le_cours_normal_du_pipeline(self, depuis, vers):
        assert refusal_reason(depuis.value, vers) is None

    def test_reecrire_le_MEME_etat_n_est_pas_une_faute(self):
        """Plusieurs chemins publient le même état ; en faire une erreur ferait échouer des
        reprises parfaitement saines."""
        assert refusal_reason(JobState.COMPLETED.value, JobState.COMPLETED) is None

    def test_un_etat_INCONNU_ne_bloque_rien(self):
        """Migration, écriture extérieure : se montrer strict face à l'inconnu transformerait
        chaque évolution du modèle en panne de production."""
        assert refusal_reason("un_etat_du_futur", JobState.COMPLETED) is None
        assert refusal_reason(None, JobState.CREATED) is None

    def test_un_echec_n_est_PAS_terminal(self):
        """Le produit présente un job échoué comme relançable, et la réconciliation au
        démarrage s'en sert. L'y mettre obligerait à forcer sur le chemin le plus courant —
        donc à ne plus rien protéger."""
        assert JobState.FAILED not in TERMINAL


class TestCeQuiEstRefuse:
    @pytest.mark.parametrize("vers", [
        JobState.CREATED, JobState.TRANSCRIBING, JobState.READY_TO_PROCESS,
        JobState.SUMMARY_RUNNING,
    ])
    def test_un_job_TERMINE_ne_repart_pas_seul(self, vers):
        raison = refusal_reason(JobState.COMPLETED.value, vers)
        assert raison and "relance explicite" in raison

    def test_un_job_ANNULE_non_plus(self):
        assert refusal_reason(JobState.CANCELLED.value, JobState.TRANSCRIBING) is not None

    def test_la_levée_nomme_les_deux_états(self):
        """Un message qui ne dit pas d'où l'on vient oblige à relire le code pour comprendre."""
        with pytest.raises(InvalidTransition) as exc:
            ensure_allowed(JobState.COMPLETED.value, JobState.TRANSCRIBING)
        assert "completed" in str(exc.value) and "transcribing" in str(exc.value)

    @pytest.mark.parametrize("vers", sorted(FROM_TERMINAL, key=lambda s: s.value))
    def test_les_sorties_ADMISES_d_un_terminal_passent(self, vers):
        """Annuler un job terminé, ou constater un échec tardif (nettoyage, réconciliation),
        restent des gestes légitimes."""
        assert refusal_reason(JobState.COMPLETED.value, vers) is None


class TestPorteUnique:
    def test_force_est_le_seul_moyen_de_repartir_d_un_terminal(self, app):
        """`force=True` doit rester RARE et visible : c'est ce qui distingue une relance
        demandée par l'utilisateur d'un effet de bord."""
        from transcria.auth.models import Role
        from transcria.auth.store import UserStore
        from transcria.jobs.store import JobStore

        with app.app_context():
            proprietaire = UserStore.create_user(f"transitions-{uuid.uuid4().hex[:8]}",
                                                 "x" * 24, role=Role.OPERATOR)
            job = JobStore.create_job(owner_id=proprietaire.id, title="transition d'état")
            JobStore.update_state(job.id, JobState.COMPLETED)
            with pytest.raises(InvalidTransition):
                JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
            relance = JobStore.update_state(job.id, JobState.READY_TO_PROCESS, force=True)
            assert relance is not None and relance.state == JobState.READY_TO_PROCESS.value

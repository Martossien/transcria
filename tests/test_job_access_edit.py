"""Passe sécurité S1.5 — lire un job et le MODIFIER ne doivent plus passer par la même porte.

`job_access.can_access_job` répondait à une seule question : « cette personne a-t-elle le
droit de voir ce job ? » — propriétaire, admin, ou membre d'un groupe commun. Toutes les
routes mutantes s'en contentaient : réécrire le SRT, changer le contexte, relancer un
traitement, consommer le GPU.

Conséquence concrète : un compte de rôle **VIEWER**, dont la seule permission est
`DOWNLOAD_EXPORTS`, pouvait réécrire le sous-titrage d'un job qui ne lui appartient pas dès
lors qu'il partageait un groupe avec le propriétaire. Le rôle disait « lecture seule », le
code disait autre chose.

Ces tests décrivent la règle voulue. Ils échouent sans le correctif.
"""
from __future__ import annotations

import uuid

import pytest

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.web.job_access import can_access_job, can_edit_job

MDP = "mot-de-passe-de-test-1"


@pytest.fixture()
def equipe(app):
    """Un propriétaire et trois collègues du MÊME groupe, un par rôle.

    Identifiants uniques par test : la base de la fixture `app` est partagée par toute la
    session, deux tests qui créeraient « viewer-s15 » se marcheraient dessus."""
    suffixe = uuid.uuid4().hex[:8]
    with app.app_context():
        membres, noms = {}, {}
        proprietaire = UserStore.create_user(f"proprio-{suffixe}", MDP, role=Role.OPERATOR)
        groupe = GroupStore.create_group(f"equipe-{suffixe}")
        GroupStore.add_member(groupe.id, proprietaire.id)
        for role in (Role.VIEWER, Role.OPERATOR, Role.MANAGER):
            nom = f"{role.value}-{suffixe}"
            u = UserStore.create_user(nom, MDP, role=role)
            GroupStore.add_member(groupe.id, u.id)
            membres[role], noms[role] = u, nom
        etranger = UserStore.create_user(f"etranger-{suffixe}", MDP, role=Role.MANAGER)
        admin = UserStore.create_user(f"admin-{suffixe}", MDP, role=Role.ADMIN)
        job = JobStore.create_job(proprietaire.id, "Job partagé")
        JobStore.update_state(job.id, JobState.COMPLETED)
        yield {"job": JobStore.get_by_id(job.id), "proprietaire": proprietaire,
               "membres": membres, "noms": noms, "etranger": etranger, "admin": admin}


class TestQuiPeutLIRE:
    """La lecture ne change pas : c'est le contrat existant, il doit être préservé."""

    def test_le_groupe_entier_voit_le_job(self, app, equipe):
        with app.app_context():
            for role, membre in equipe["membres"].items():
                assert can_access_job(equipe["job"], membre), role

    def test_un_etranger_ne_voit_rien(self, app, equipe):
        with app.app_context():
            assert not can_access_job(equipe["job"], equipe["etranger"])


class TestQuiPeutMODIFIER:
    def test_un_VIEWER_du_groupe_ne_peut_PAS_modifier(self, app, equipe):
        """Le cœur du sujet : il voit, il télécharge, il ne réécrit pas."""
        with app.app_context():
            viewer = equipe["membres"][Role.VIEWER]
            assert can_access_job(equipe["job"], viewer)        # il voit…
            assert not can_edit_job(equipe["job"], viewer)      # …mais c'est tout

    @pytest.mark.parametrize("role", [Role.OPERATOR, Role.MANAGER])
    def test_un_collegue_qui_produit_peut_modifier(self, app, equipe, role):
        """Le partage garde son sens : une équipe travaille sur les jobs de ses membres."""
        with app.app_context():
            assert can_edit_job(equipe["job"], equipe["membres"][role])

    def test_le_proprietaire_peut_toujours_modifier(self, app, equipe):
        with app.app_context():
            assert can_edit_job(equipe["job"], equipe["proprietaire"])

    def test_ladministrateur_peut_modifier(self, app, equipe):
        with app.app_context():
            assert can_edit_job(equipe["job"], equipe["admin"])

    def test_un_etranger_ne_peut_pas_modifier(self, app, equipe):
        with app.app_context():
            assert not can_edit_job(equipe["job"], equipe["etranger"])


class TestLesRoutesMutantes:
    """La garde ne vaut que si les routes l'utilisent — sinon c'est une fonction morte."""

    def _connecte(self, client, nom):
        return client.post("/login", data={"username": nom, "password": MDP},
                           follow_redirects=True)

    def test_un_VIEWER_ne_peut_pas_reecrire_le_SRT(self, app, client, equipe):
        """L'éditeur SRT est le cas le plus net : il RÉÉCRIT le livrable."""
        self._connecte(client, equipe["noms"][Role.VIEWER])
        job_id = equipe["job"].id
        r = client.put(f"/api/jobs/{job_id}/editor/draft",
                       json={"srt": "1\n00:00:00,000 --> 00:00:01,000\nfalsifié\n"})
        assert r.status_code == 403

    def test_un_VIEWER_ne_peut_pas_relancer_un_traitement(self, app, client, equipe):
        """Relancer consomme le GPU du groupe : ce n'est pas de la lecture."""
        self._connecte(client, equipe["noms"][Role.VIEWER])
        r = client.post(f"/api/jobs/{equipe['job'].id}/reprocess", json={})
        assert r.status_code == 403

    def test_un_VIEWER_ne_peut_pas_changer_le_contexte(self, app, client, equipe):
        self._connecte(client, equipe["noms"][Role.VIEWER])
        r = client.post(f"/api/jobs/{equipe['job'].id}/context", json={"context": "réécrit"})
        assert r.status_code == 403

    def test_un_VIEWER_LIT_toujours(self, app, client, equipe):
        """Contre-épreuve indispensable : on n'a pas simplement tout fermé.

        (Route d'état, et non l'éditeur : celui-ci répond 404 tant qu'aucun sous-titrage
        n'existe, ce qui ne dirait rien des droits.)"""
        self._connecte(client, equipe["noms"][Role.VIEWER])
        assert client.get(f"/api/jobs/{equipe['job'].id}/status").status_code == 200

    def test_un_OPERATOR_du_groupe_garde_ses_droits(self, app, client, equipe):
        """Contre-épreuve : le partage doit continuer de fonctionner."""
        self._connecte(client, equipe["noms"][Role.OPERATOR])
        r = client.post(f"/api/jobs/{equipe['job'].id}/context", json={"context": "légitime"})
        assert r.status_code != 403

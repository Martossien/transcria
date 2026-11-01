"""Bandeau « N réunions attendent votre validation » (retours utilisateurs 2026-11).

La pré-analyse se lance seule (autostart) puis le wizard attend l'utilisateur ; sans
e-mail configuré, rien ne le lui disait. Le bandeau compte les jobs de L'UTILISATEUR
dans les états d'attente et s'affiche sur toutes les pages sauf le wizard lui-même.
"""
from __future__ import annotations

from builders import make_job

from transcria.auth.store import UserStore
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore


def _admin_id(app):
    with app.app_context():
        return UserStore.get_by_username("admin").id


def test_count_ne_compte_que_les_etats_d_attente_du_proprietaire(app):
    with app.app_context():
        owner = _admin_id(app)
        make_job(owner, state=JobState.SUMMARY_DONE)
        make_job(owner, state=JobState.LEXICON_DONE)
        make_job(owner, state=JobState.COMPLETED)
        make_job(owner, state=JobState.TRANSCRIBING)
        other = UserStore.create_user(username="autre", password="x" * 8, display_name="Autre")
        make_job(other.id, state=JobState.SUMMARY_DONE)
        assert JobStore.count_awaiting_validation(owner) == 2
        assert JobStore.count_awaiting_validation(other.id) == 1


def test_bandeau_sur_l_accueil_pas_sur_le_wizard(app, admin_client):
    with app.app_context():
        job = make_job(_admin_id(app), state=JobState.SUMMARY_DONE)
        job_id = job.id
    home = admin_client.get("/").get_data(as_text=True)
    # « attend » au singulier, « attendent » au pluriel : on teste le fragment commun.
    assert "data-awaiting-validation" in home and "votre validation" in home
    wizard = admin_client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "data-awaiting-validation" not in wizard
    assert "data-awaiting-user" in wizard  # la ligne « rien ne se lance sans vous » du wizard


def test_sans_job_en_attente_pas_de_bandeau(app):
    # Base partagée entre tests (fixture de session) : un utilisateur NEUF, sans aucun job.
    with app.app_context():
        UserStore.create_user(username="sans-attente", password="x" * 8, display_name="Vide")
    c = app.test_client()
    c.post("/login", data={"username": "sans-attente", "password": "x" * 8}, follow_redirects=True)
    assert "data-awaiting-validation" not in c.get("/").get_data(as_text=True)


def test_count_active_without_email(app):
    with app.app_context():
        before = UserStore.count_active_without_email()
        UserStore.create_user(username="sans-mail", password="x" * 8, display_name="S")
        UserStore.create_user(username="avec-mail", password="x" * 8, display_name="A", email="a@example.org")
        assert UserStore.count_active_without_email() == before + 1

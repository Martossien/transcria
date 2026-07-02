"""Phase 2 — circulation de `processing_profile_id` (API + exécution).

Vérifie que le profil de traitement circule sans casser `mode` (unité d'exécution) :
persistance dans `extra_data.execution`, préservation au re-queue, et résolution dans les
routes `api_process`/`api_reprocess` (legacy `fast`/`quality` + profil explicite).
"""
from __future__ import annotations

import io

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.services.job_executor import get_job_executor
from transcria.workflow.transitions import mark_execution_queued


def _uploaded_job(admin_client) -> str:
    r = admin_client.post("/jobs/new", data={"title": "Profil"}, follow_redirects=True)
    jid = r.request.path.rstrip("/").split("/")[-1]
    r = admin_client.post(
        f"/api/jobs/{jid}/upload",
        data={"file": (io.BytesIO(b"fake audio"), "audio.mp3")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    return jid


# ── Niveau exécution : persistance + préservation au re-queue ────────────────--

def test_mark_execution_queued_persiste_le_profil(app, owner_id):
    with app.app_context():
        job = JobStore.create_job(owner_id, "Exec profil")
        mark_execution_queued(job.id, "quality", "word_corrige")
        execution = JobStore.get_by_id(job.id).get_extra_data()["execution"]
        assert execution["mode"] == "quality"
        assert execution["processing_profile_id"] == "word_corrige"


def test_requeue_sans_profil_ne_lecrase_pas(app, owner_id):
    # Un re-queue automatique (vram_wait/deferred) ne repasse pas le profil : il doit survivre.
    with app.app_context():
        job = JobStore.create_job(owner_id, "Re-queue")
        mark_execution_queued(job.id, "quality", "dossier_qualite")
        mark_execution_queued(job.id, "quality")  # re-queue, sans profil
        execution = JobStore.get_by_id(job.id).get_extra_data()["execution"]
        assert execution["processing_profile_id"] == "dossier_qualite"


# ── Routes : résolution profil/legacy + threading vers submit_process ────────--

def _capture_submit(monkeypatch):
    captured: dict = {}
    executor = get_job_executor()

    def fake_submit(job_id, audio_path, mode, **kwargs):
        captured["mode"] = mode
        captured["processing_profile_id"] = kwargs.get("processing_profile_id")
        return {"accepted": True, "status": "queued", "mode": mode}

    monkeypatch.setattr(executor, "submit_process", fake_submit)
    return captured


def test_process_profil_explicite_threade(admin_client, app, monkeypatch):
    # srt_locuteurs route en `fast` (ne diarise pas) → évite le gate quality (désactivé en test).
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.READY_TO_PROCESS)
    captured = _capture_submit(monkeypatch)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"processing_profile_id": "srt_locuteurs"})

    assert r.status_code == 202
    body = r.get_json()
    assert body["processing_profile_id"] == "srt_locuteurs"
    assert body["mode"] == "fast"
    assert captured["processing_profile_id"] == "srt_locuteurs"
    assert captured["mode"] == "fast"


def test_process_profil_quality_bloque_si_quality_desactive(admin_client, app, monkeypatch):
    # Le gate `enable_quality_mode` doit s'appliquer au mode DÉRIVÉ du profil : word_corrige
    # diarise → routage quality → refus propre. On force l'état dans la config vivante (singleton
    # process, mutable par d'autres tests) pour être indépendant de l'ordre d'exécution.
    from transcria.config import get_config

    monkeypatch.setitem(get_config()["workflow"], "enable_quality_mode", False)
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.READY_TO_PROCESS)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"processing_profile_id": "word_corrige"})

    assert r.status_code == 400
    assert "qualité" in r.get_json()["error"]


def test_process_legacy_fast_mappe_vers_legacy_fast(admin_client, app, monkeypatch):
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.READY_TO_PROCESS)
    captured = _capture_submit(monkeypatch)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"mode": "fast"})

    assert r.status_code == 202
    body = r.get_json()
    assert body["processing_profile_id"] == "legacy_fast"
    assert body["mode"] == "fast"
    assert captured["processing_profile_id"] == "legacy_fast"


def test_srt_express_lancable_des_analyzed(admin_client, app, monkeypatch):
    # Reflet du code : srt_express n'exige aucune validation humaine → lançable après l'analyse,
    # sans résumé/contexte/participants/lexique.
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.ANALYZED)
    captured = _capture_submit(monkeypatch)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"processing_profile_id": "srt_express"})

    assert r.status_code == 202
    assert captured["processing_profile_id"] == "srt_express"


def test_word_rapide_refuse_des_analyzed(admin_client, app):
    # word_rapide route en `fast` (passe le gate qualité) mais exige résumé+contexte
    # → 409 tant que ces étapes ne sont pas validées.
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.ANALYZED)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"processing_profile_id": "word_rapide"})

    assert r.status_code == 409
    assert r.get_json()["processing_profile_id"] == "word_rapide"


def test_process_profil_inconnu_rejete(admin_client, app):
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.READY_TO_PROCESS)

    r = admin_client.post(f"/api/jobs/{jid}/process", json={"processing_profile_id": "inexistant"})

    assert r.status_code == 400
    assert "invalide" in r.get_json()["error"]


def test_endpoint_disponibilite_profils(admin_client):
    r = admin_client.get("/api/profiles/availability")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["profiles"]) == 6
    ids = {p["id"] for p in body["profiles"]}
    assert "dossier_qualite" in ids and "srt_express" in ids
    for p in body["profiles"]:
        assert set(p) >= {"id", "label", "status", "available", "deliverables", "validations"}
    assert body["recommended"] in ids or body["recommended"] is None


def test_wizard_rend_le_selecteur_de_profils(admin_client, app):
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.LEXICON_DONE)
    r = admin_client.get(f"/jobs/{jid}")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="profile-selector"' in html
    assert 'id="profiles-data"' in html
    assert "Lancer le traitement" in html


def test_wizard_profil_verrouille_avant_upload(admin_client):
    # Le profil se choisit APRÈS le téléversement : avant, toutes les pastilles
    # sont désactivées et une note explique le verrou.
    r = admin_client.post("/jobs/new", data={"title": "Sans fichier"}, follow_redirects=True)
    jid = r.request.path.rstrip("/").split("/")[-1]
    html = admin_client.get(f"/jobs/{jid}").data.decode()
    assert "Téléversez d'abord votre fichier" in html
    # Chaque pastille rendue est désactivée (verrou upload ou indisponibilité).
    assert html.count("data-profile-id=") == html.count('disabled aria-disabled="true"')


def test_wizard_profil_actif_apres_upload(admin_client):
    jid = _uploaded_job(admin_client)
    html = admin_client.get(f"/jobs/{jid}").data.decode()
    assert "Téléversez d'abord votre fichier" not in html


def test_reprocess_profil_explicite_threade(admin_client, app, monkeypatch):
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.COMPLETED)
    captured = _capture_submit(monkeypatch)

    r = admin_client.post(f"/api/jobs/{jid}/reprocess", json={"processing_profile_id": "dossier_qualite"})

    assert r.status_code == 202
    body = r.get_json()
    assert body["processing_profile_id"] == "dossier_qualite"
    assert body["reprocess"] is True
    assert captured["processing_profile_id"] == "dossier_qualite"


# ── Choix du profil à l'étape 1 : persistance sans lancement ─────────────────--

def test_set_profile_persiste_sans_lancer(admin_client, app):
    # Choisir le profil à l'étape 1 le mémorise dans extra_data SANS enfiler le job.
    jid = _uploaded_job(admin_client)

    r = admin_client.post(f"/api/jobs/{jid}/profile", json={"processing_profile_id": "srt_express"})

    assert r.status_code == 200
    assert r.get_json()["processing_profile_id"] == "srt_express"
    with app.app_context():
        extra = JobStore.get_by_id(jid).get_extra_data()
        assert extra["execution"]["processing_profile_id"] == "srt_express"
        # Pas de lancement : aucun statut d'exécution actif posé.
        assert extra["execution"].get("status") is None


def test_set_profile_invalide_rejete(admin_client, app):
    jid = _uploaded_job(admin_client)

    r = admin_client.post(f"/api/jobs/{jid}/profile", json={"processing_profile_id": "inexistant"})

    assert r.status_code == 400
    assert "invalide" in r.get_json()["error"]


def test_set_profile_indisponible_refuse(admin_client, app, monkeypatch):
    # Un profil structurellement indisponible (mode qualité coupé) ne peut être retenu.
    from transcria.config import get_config

    monkeypatch.setitem(get_config()["workflow"], "enable_quality_mode", False)
    jid = _uploaded_job(admin_client)

    r = admin_client.post(f"/api/jobs/{jid}/profile", json={"processing_profile_id": "dossier_qualite"})

    assert r.status_code == 409
    assert r.get_json()["processing_profile_id"] == "dossier_qualite"


def test_wizard_rend_avec_srt_express_persiste(admin_client, app):
    # Rendu réel : profil léger persisté → page affiche le bouton de lancement, pas d'erreur Jinja.
    jid = _uploaded_job(admin_client)
    with app.app_context():
        JobStore.update_state(jid, JobState.ANALYZED)
    admin_client.post(f"/api/jobs/{jid}/profile", json={"processing_profile_id": "srt_express"})

    r = admin_client.get(f"/jobs/{jid}")

    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Lancer le traitement" in html
    assert "Fichier audio &amp; profil de traitement" in html

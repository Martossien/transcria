"""Champs additifs `queue_position`/`wait_estimate` du contrat ⭐ /api/jobs/<id>/status.

PISTES_AMELIORATION §5.4 : la position en file et l'estimation calibrée machine
(déjà calculées pour /admin/queue) sont exposées au propriétaire du job pendant
l'attente — et SEULEMENT pendant l'attente (jamais pour un job hors file).
"""
from __future__ import annotations

import pytest
from builders import make_job

from transcria.jobs.models import JobState
from transcria.web import processing_api


@pytest.fixture
def waiting_job(app, owner_id):
    with app.app_context():
        return make_job(owner_id, state=JobState.READY_TO_PROCESS).id


class TestQueueWaitInfo:
    def test_job_hors_file_na_pas_les_champs(self, app, admin_client, waiting_job):
        resp = admin_client.get(f"/api/jobs/{waiting_job}/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "queue_position" not in data
        assert "wait_estimate" not in data

    def test_job_en_file_expose_position_et_estimation(self, app, admin_client, waiting_job, monkeypatch):
        # Coutures au CONSOMMATEUR (doctrine B2) : le calcul réel est celui de
        # /admin/queue, ici on vérifie le branchement et la forme du contrat.
        monkeypatch.setattr(processing_api.QueueStore, "get_position", staticmethod(lambda job_id: 3))
        monkeypatch.setattr(processing_api.QueueStore, "get_ordered_queue",
                            staticmethod(lambda limit=100, include_running=False: []))
        monkeypatch.setattr(processing_api, "queue_wait_estimates",
                            lambda cfg, entries: {waiting_job: {"seconds": 720, "text": "12 min"}})

        data = admin_client.get(f"/api/jobs/{waiting_job}/status").get_json()
        assert data["queue_position"] == 3
        assert data["wait_estimate"] == {"seconds": 720, "text": "12 min"}
        # les champs historiques du contrat ⭐ restent inchangés
        for key in ("state", "execution_status", "progress", "eta"):
            assert key in data

    def test_position_sans_estimation_reste_valide(self, app, admin_client, waiting_job, monkeypatch):
        monkeypatch.setattr(processing_api.QueueStore, "get_position", staticmethod(lambda job_id: 1))
        monkeypatch.setattr(processing_api.QueueStore, "get_ordered_queue",
                            staticmethod(lambda limit=100, include_running=False: []))
        monkeypatch.setattr(processing_api, "queue_wait_estimates", lambda cfg, entries: {})

        data = admin_client.get(f"/api/jobs/{waiting_job}/status").get_json()
        assert data["queue_position"] == 1
        assert "wait_estimate" not in data

    def test_echec_du_calcul_ne_casse_jamais_le_statut(self, app, admin_client, waiting_job, monkeypatch):
        def _boom(job_id):
            raise RuntimeError("base indisponible")

        monkeypatch.setattr(processing_api.QueueStore, "get_position", staticmethod(_boom))
        resp = admin_client.get(f"/api/jobs/{waiting_job}/status")
        assert resp.status_code == 200
        assert "queue_position" not in resp.get_json()

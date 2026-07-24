"""Micro direct (record-then-transcribe) — chemin backend : upload d'un blob webm
marqué source=mic passe par le flux de job normal.

Le front (MediaRecorder) est couvert par le walkthrough Playzright ; ici on prouve
que le backend accepte le conteneur webm et trace la provenance micro.
"""
from __future__ import annotations

import io

import pytest

from transcria.config.loader import get_default_config
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore


def test_webm_dans_les_extensions_par_defaut():
    assert ".webm" in get_default_config()["security"]["allowed_upload_extensions"]


def test_get_original_audio_path_detecte_webm(tmp_path):
    fs = JobFilesystem(str(tmp_path), "job-mic")
    (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
    (fs.job_dir / "input" / "micro-123.webm").write_bytes(b"\x1aE\xdf\xa3")  # entête EBML/webm
    found = fs.get_original_audio_path()
    assert found is not None and found.suffix == ".webm"


@pytest.fixture
def allow_webm(app):
    """Rend l'upload hermétique à la config ambiante (config.yaml peut pinner une
    liste sans .webm) : garantit .webm autorisé le temps du test, puis restaure."""
    from transcria.config import get_config
    exts = get_config()["security"]["allowed_upload_extensions"]
    added = ".webm" not in exts
    if added:
        exts.append(".webm")
    yield
    if added:
        exts.remove(".webm")


def _new_job(admin_client) -> str:
    r = admin_client.post("/jobs/new", data={"title": "Micro"}, follow_redirects=True)
    return r.request.path.split("/")[2]


class TestUploadMic:
    def test_upload_webm_source_mic_accepte_et_trace(self, app, admin_client, allow_webm):
        job_id = _new_job(admin_client)
        r = admin_client.post(
            f"/api/jobs/{job_id}/upload",
            data={"file": (io.BytesIO(b"\x1aE\xdf\xa3ballast"), "micro-42.webm"), "source": "mic"},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        assert not r.get_json().get("error")
        with app.app_context():
            job = JobStore.get_by_id(job_id)
            assert job.get_extra_data().get("source") == "mic"

    def test_upload_fichier_classique_ne_trace_pas_de_source(self, app, admin_client):
        # Défaut inchangé : un upload sans champ source ne pose PAS extra_data["source"].
        job_id = _new_job(admin_client)
        r = admin_client.post(
            f"/api/jobs/{job_id}/upload",
            data={"file": (io.BytesIO(b"RIFFxxxxWAVE"), "reunion.wav")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        with app.app_context():
            assert "source" not in JobStore.get_by_id(job_id).get_extra_data()

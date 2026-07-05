"""Tests des routes d'upload/suppression des documents joints au résumé.

Vérifient le canal HTTP (validation format, stockage dans ``meeting_invite.documents``,
préservation vis-à-vis du texte collé, suppression) sans GPU ni LLM.
"""
from __future__ import annotations

import io

import docx


def _make_docx(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_job(admin_client) -> str:
    r = admin_client.post("/jobs/new", data={"title": "Doc Test"}, follow_redirects=True)
    path = r.request.path
    return path.split("/")[2] if "/jobs/" in path else ""


def _meeting_invite(app, job_id: str) -> dict:
    with app.app_context():
        from transcria.jobs.store import JobStore

        job = JobStore.get_by_id(job_id)
        return (job.get_extra_data() or {}).get("meeting_invite") or {}


def test_upload_document_stores_extracted_text(admin_client, app):
    job_id = _make_job(admin_client)
    assert job_id
    data = _make_docx("Ordre du jour du comité de pilotage")
    r = admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(data), "odj.docx")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["name"] == "odj.docx"
    assert body["documents"][0]["format"] == "docx"

    invite = _meeting_invite(app, job_id)
    assert invite["documents"][0]["text"]
    assert "comité de pilotage" in invite["documents"][0]["text"]


def test_upload_rejects_unsupported_format(admin_client):
    job_id = _make_job(admin_client)
    r = admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(b"vieux binaire"), "presentation.ppt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert "non géré" in r.get_json()["error"].lower() or ".ppt" in r.get_json()["error"]


def test_upload_rejects_missing_file(admin_client):
    job_id = _make_job(admin_client)
    r = admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={}, content_type="multipart/form-data",
    )
    assert r.status_code == 400


def test_upload_rejects_beyond_max_documents(admin_client, app, monkeypatch):
    job_id = _make_job(admin_client)
    with app.app_context():
        from transcria.config import get_config
        real = get_config()
    patched = {**real, "security": {**real["security"], "max_documents_per_job": 2}}
    monkeypatch.setattr("transcria.web.routes.get_config", lambda: patched)

    for i in range(2):
        r = admin_client.post(
            f"/api/jobs/{job_id}/meeting-invite/document",
            data={"file": (io.BytesIO(_make_docx(f"doc {i}")), f"d{i}.docx")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
    # Le 3ᵉ dépasse le cap → rejeté avant lecture du fichier.
    r = admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(_make_docx("over")), "over.docx")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert "max 2" in r.get_json()["error"].lower()


def test_text_invite_preserves_documents(admin_client, app):
    job_id = _make_job(admin_client)
    admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(_make_docx("Support annexe")), "annexe.docx")},
        content_type="multipart/form-data",
    )
    # Poster du texte d'invitation ne doit PAS écraser les documents joints.
    r = admin_client.post(f"/api/jobs/{job_id}/meeting-invite", json={"text": "Objet : bilan"})
    assert r.status_code == 200
    invite = _meeting_invite(app, job_id)
    assert invite.get("brief")
    assert len(invite.get("documents") or []) == 1
    assert invite["documents"][0]["name"] == "annexe.docx"


def test_delete_document(admin_client, app):
    job_id = _make_job(admin_client)
    admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(_make_docx("A")), "a.docx")},
        content_type="multipart/form-data",
    )
    admin_client.post(
        f"/api/jobs/{job_id}/meeting-invite/document",
        data={"file": (io.BytesIO(_make_docx("B")), "b.docx")},
        content_type="multipart/form-data",
    )
    r = admin_client.delete(f"/api/jobs/{job_id}/meeting-invite/document/0")
    assert r.status_code == 200
    docs = r.get_json()["documents"]
    assert len(docs) == 1
    assert docs[0]["name"] == "b.docx"


def test_delete_document_bad_index_404(admin_client):
    job_id = _make_job(admin_client)
    r = admin_client.delete(f"/api/jobs/{job_id}/meeting-invite/document/5")
    assert r.status_code == 404

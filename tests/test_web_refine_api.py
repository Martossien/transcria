"""API du chat d'affinage des livrables — RBAC, gardes, enfilage, revert, options.

Le POST /refine écrit ``refine/request.json`` puis enfile un tour ``mode=refine``
(exécuteur mocké : on vérifie l'ENFILAGE, pas l'exécution — la phase est testée dans
``test_run_refine.py``). Le GET /refine/chat est l'endpoint de polling unique de l'UI
(tours + busy + versions + options de rendu courantes).
"""
import json

import pytest
from test_docx_route import _seed_job_files


@pytest.fixture
def refine_job(admin_client, app):
    """Job COMPLETED avec fichiers de livrables + chat vide."""
    r = admin_client.post("/jobs/new", data={"title": "Test refine"}, follow_redirects=True)
    job_id = r.request.path.split("/")[2]

    from transcria.config import get_config
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore

    with app.app_context():
        cfg = get_config()
        _seed_job_files(cfg["storage"]["jobs_dir"], job_id)
        JobStore.update_state(job_id, JobState.COMPLETED)
    return job_id


class _FakeExecutor:
    def __init__(self, accepted: bool = True):
        self.accepted = accepted
        self.submitted: list[tuple] = []

    def submit_process(self, job_id, audio_path, mode, **kwargs):
        self.submitted.append((job_id, mode))
        return {"accepted": self.accepted, "status": "queued", "mode": mode}


@pytest.fixture
def fake_executor(monkeypatch):
    fake = _FakeExecutor()
    import transcria.web.refine_api as routes
    monkeypatch.setattr(routes, "get_job_executor", lambda: fake)
    return fake


class TestRefineSubmit:
    def test_requires_login(self, client):
        r = client.post("/api/jobs/x/refine", json={"kind": "discuss", "message": "?"})
        assert r.status_code in (302, 401)

    def test_non_owner_forbidden(self, operator_client, refine_job):
        r = operator_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "?"})
        assert r.status_code == 403

    def test_submit_discuss_202_and_enqueued(self, admin_client, app, refine_job, fake_executor):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine",
                              json={"kind": "discuss", "message": "De quoi parle la réunion ?"})
        assert r.status_code == 202
        assert fake_executor.submitted == [(refine_job, "refine")]
        # La demande est posée pour le worker.
        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore
        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=refine_job)
            assert store.has_active_request() is True

    def test_empty_message_400(self, admin_client, refine_job, fake_executor):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "  "})
        assert r.status_code == 400

    def test_bad_kind_400(self, admin_client, refine_job, fake_executor):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "delete", "message": "x"})
        assert r.status_code == 400

    def test_too_long_message_400(self, admin_client, refine_job, fake_executor):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine",
                              json={"kind": "discuss", "message": "x" * 5000})
        assert r.status_code == 400

    def test_busy_409(self, admin_client, app, refine_job, fake_executor):
        admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "a"})
        r = admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "b"})
        assert r.status_code == 409

    def test_job_not_completed_409(self, admin_client, app, refine_job, fake_executor):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        with app.app_context():
            JobStore.update_state(refine_job, JobState.CREATED)
        r = admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "?"})
        assert r.status_code == 409

    def test_rejected_enqueue_cleans_request(self, admin_client, app, refine_job, monkeypatch):
        fake = _FakeExecutor(accepted=False)
        import transcria.web.refine_api as routes
        monkeypatch.setattr(routes, "get_job_executor", lambda: fake)
        r = admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "apply", "message": "x"})
        assert r.status_code == 409
        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore
        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=refine_job)
            assert store.has_active_request() is False   # pas de demande fantôme


class TestRefineChat:
    def test_chat_empty(self, admin_client, refine_job):
        r = admin_client.get(f"/api/jobs/{refine_job}/refine/chat")
        assert r.status_code == 200
        data = r.get_json()
        assert data["turns"] == [] and data["busy"] is False and data["versions"] == []
        assert "themes" in data and "render_options" in data   # pour les sélecteurs UI

    def test_chat_returns_turns_and_busy(self, admin_client, app, refine_job, fake_executor):
        admin_client.post(f"/api/jobs/{refine_job}/refine", json={"kind": "discuss", "message": "Question ?"})
        r = admin_client.get(f"/api/jobs/{refine_job}/refine/chat")
        data = r.get_json()
        assert data["busy"] is True   # demande en attente de traitement

    def test_chat_requires_access(self, operator_client, refine_job):
        assert operator_client.get(f"/api/jobs/{refine_job}/refine/chat").status_code == 403


class TestRenderOptionsDirect:
    """Options de rendu déterministes SANS LLM (instantané, zéro VRAM)."""

    def test_set_options_writes_and_versions(self, admin_client, app, refine_job):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine/render-options",
                              json={"theme": "CSE", "sections": {"transcript": False}})
        assert r.status_code == 200
        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem
        with app.app_context():
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], refine_job)
            opts = fs.load_json("context/render_options.json")
        assert opts == {"theme": "CSE", "sections": {"transcript": False}}

    def test_invalid_options_400(self, admin_client, refine_job):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine/render-options",
                              json={"theme": "zzz", "sections": "junk"})
        assert r.status_code == 400

    def test_docx_reflects_options(self, admin_client, app, refine_job):
        pytest.importorskip("docx")
        import io

        from docx import Document
        admin_client.post(f"/api/jobs/{refine_job}/refine/render-options",
                          json={"sections": {"transcript": False}})
        r = admin_client.get(f"/api/jobs/{refine_job}/download/docx")
        doc = Document(io.BytesIO(r.data))
        heads = [p.text for p in doc.paragraphs if p.text.strip().split(".")[0].isdigit()]
        assert not any("TRANSCRIPTION" in h for h in heads)


class TestRefineRevert:
    def test_revert_restores_snapshot(self, admin_client, app, refine_job):
        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.refine_store import RefineStore

        with app.app_context():
            cfg = get_config()
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], refine_job)
            store = RefineStore(jobs_dir=cfg["storage"]["jobs_dir"], job_id=refine_job)
            original = fs.load_json("context/meeting_context.json")
            store.snapshot_artifacts([fs.job_dir / "context" / "meeting_context.json"])
            modified = dict(original); modified["summary"] = "Synthèse modifiée par apply."
            fs.save_json("context/meeting_context.json", modified)

        r = admin_client.post(f"/api/jobs/{refine_job}/refine/revert", json={"version": 1})
        assert r.status_code == 200
        with app.app_context():
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], refine_job)
            assert fs.load_json("context/meeting_context.json") == original

    def test_revert_unknown_version_404(self, admin_client, refine_job):
        r = admin_client.post(f"/api/jobs/{refine_job}/refine/revert", json={"version": 42})
        assert r.status_code == 404


class TestResultPagePanel:
    def test_chat_panel_present_on_result_page(self, admin_client, app, refine_job):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        with app.app_context():
            JobStore.update_state(refine_job, JobState.COMPLETED)
        r = admin_client.get(f"/jobs/{refine_job}/result")
        body = r.data.decode("utf-8")
        assert "refine-chat" in body           # panneau présent
        assert "refine-thread" in body         # fil de discussion
        assert "refine-apply" in body          # bouton Appliquer
        assert f'data-job-id="{refine_job}"' in body  # endpoint câblé sur le job
        assert "refine-fresh-note" in body     # note « documents à jour » (post-apply)

    def test_result_page_previews_corrected_srt(self, admin_client, app, refine_job):
        # L'aperçu à l'écran montre le SRT CORRIGÉ (ce que /download/srt sert) — pas le
        # brut : sinon les corrections (LLM/affinage) semblent « ne pas être appliquées ».
        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem
        with app.app_context():
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], refine_job)
            fs.save_text("metadata/transcription.srt",
                         "1\n00:00:00,000 --> 00:00:01,000\nVERSION BRUTE NON CORRIGÉE\n")
        html = admin_client.get(f"/jobs/{refine_job}/result").data.decode()
        assert "VERSION BRUTE NON CORRIGÉE" not in html
        assert "Bonjour à tous, on commence la réunion." in html   # SRT corrigé affiché

    def test_wizard_links_to_result_page(self, admin_client, refine_job):
        # La page résultats (qui porte le chat d'affinage) doit être atteignable
        # depuis le wizard une fois le traitement terminé (étape Export).
        html = admin_client.get(f"/jobs/{refine_job}").data.decode()
        assert f"/jobs/{refine_job}/result" in html

    def test_home_links_to_result_page(self, admin_client, refine_job):
        # ... et depuis la liste des traitements de l'accueil (job terminé).
        html = admin_client.get("/").data.decode()
        assert f"/jobs/{refine_job}/result" in html

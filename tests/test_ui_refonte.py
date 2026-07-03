"""Refonte de l'interface web (docs/archive/REFONTE_UI.md).

Couvre : libellés français des états (plus d'état brut à l'écran), stepper du wizard
sans troncature, bandeau d'échec avec error_message + relance, onglets Prompts LLM
(édition sécurisée : liste fermée, backup, garde non-vide) et Scripts (lecture seule),
page Système consciente du rôle + carte stockage.
"""
from __future__ import annotations

from transcria.config import get_config
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.web.ui_labels import JOB_STATE_LABELS, state_badge, state_label


class TestStateLabels:
    def test_every_job_state_has_a_french_label(self):
        for state in JobState:
            assert state.value in JOB_STATE_LABELS, state.value
            label = state_label(state.value)
            assert label and label != state.value  # jamais l'état brut

    def test_unknown_state_falls_back_safely(self):
        assert state_label("etat_inconnu") == "etat_inconnu"
        assert state_label(None) == "inconnu"
        assert state_badge("etat_inconnu") == "text-bg-secondary"

    def test_badge_colors(self):
        assert state_badge("completed") == "text-bg-success"
        assert state_badge("failed") == "text-bg-danger"
        assert state_badge("transcribing") == "text-bg-info"
        assert state_badge("ready_to_process") == "text-bg-primary"


class TestHomeAndWizard:
    def test_home_shows_french_state_not_raw(self, app, admin_client, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Réunion libellés")
            JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
        html = admin_client.get("/").data.decode()
        assert "Prêt à traiter" in html
        assert ">ready_to_process<" not in html

    def test_wizard_stepper_shows_full_labels(self, app, admin_client, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Réunion stepper")
            job_id = job.id
        html = admin_client.get(f"/jobs/{job_id}").data.decode()
        # Avant la refonte : libellés tronqués à 10 caractères (« Participan »).
        assert "Participants" in html
        assert "Participan<" not in html

    def test_wizard_failed_banner_shows_error_and_retry(self, app, admin_client, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Réunion échec")
            JobStore.update_state(job.id, JobState.FAILED, "VRAM insuffisante pour cohere-summary")
            job_id = job.id
        html = admin_client.get(f"/jobs/{job_id}").data.decode()
        assert "Le traitement a échoué" in html
        assert "VRAM insuffisante pour cohere-summary" in html
        assert "Relancer le traitement" in html

    def test_wizard_no_failed_banner_on_healthy_job(self, app, admin_client, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Réunion saine")
            job_id = job.id
        html = admin_client.get(f"/jobs/{job_id}").data.decode()
        assert "Le traitement a échoué" not in html


class TestWizardSummaryEdit:
    """Étape 4 « Contexte » — l'édition manuelle du résumé doit atteindre les livrables.

    Régression : le textarea du résumé était rendu HORS du `<form id="context-form">`,
    donc `saveContext()` (FormData(#context-form)) ne le soumettait jamais → la clé
    `summary` restait vide et le DOCX retombait toujours sur `summary_llm` (texte brut
    de la LLM). Le contrat (cf. docx_report._meta, runner._apply_final_review) est :
    édition manuelle (`summary`) > synthèse harmonisée > synthèse brute.
    """

    def _seed_meeting(self, app, owner_id, ctx: dict) -> str:
        from transcria.jobs.filesystem import JobFilesystem
        with app.app_context():
            job = JobStore.create_job(owner_id, "Réunion résumé éditable")
            # L'étape 4 (Contexte) ne se rend qu'à partir de summary_status == 'done'.
            JobStore.update_state(job.id, JobState.SUMMARY_DONE)
            cfg = get_config()
            JobFilesystem(cfg["storage"]["jobs_dir"], job.id).save_json(
                "context/meeting_context.json", ctx
            )
            return job.id

    def test_editable_summary_is_inside_the_submitted_form(self, app, admin_client, owner_id):
        job_id = self._seed_meeting(app, owner_id, {"summary_llm": "## Synthèse\nRésumé IA initial."})
        html = admin_client.get(f"/jobs/{job_id}").data.decode()
        assert 'name="summary"' in html  # le champ est bien rendu…
        form = html.split('<form id="context-form">', 1)[1].split("</form>", 1)[0]
        assert 'name="summary"' in form  # …et DANS le formulaire que saveContext() POST

    def test_context_save_persists_edited_summary_without_losing_llm_original(self, app, admin_client, owner_id):
        from transcria.jobs.filesystem import JobFilesystem
        job_id = self._seed_meeting(app, owner_id, {"summary_llm": "Résumé IA brut."})
        resp = admin_client.post(
            f"/api/jobs/{job_id}/context",
            json={"title": "Comité", "summary": "RÉSUMÉ CORRIGÉ À LA MAIN"},
        )
        assert resp.status_code == 200
        with app.app_context():
            cfg = get_config()
            ctx = JobFilesystem(cfg["storage"]["jobs_dir"], job_id).load_json("context/meeting_context.json")
        assert ctx["summary"] == "RÉSUMÉ CORRIGÉ À LA MAIN"  # l'édition est persistée
        assert ctx["summary_llm"] == "Résumé IA brut."        # l'original LLM est préservé

    def test_docx_prefers_manual_summary_over_llm(self):
        from transcria.exports.docx_report import DocxReport
        ctx = {
            "meeting_type": "Réunion interne",
            "summary": "SYNTHESE EDITEE MANUELLEMENT",
            "summary_llm": "## Synthèse\nSYNTHESE BRUTE DE LA LLM",
        }
        srt = "1\n00:00:00,000 --> 00:00:01,000\nBonjour\n"
        doc = DocxReport(ctx, [], {}, {}, srt).build()
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "SYNTHESE EDITEE MANUELLEMENT" in text
        assert "SYNTHESE BRUTE DE LA LLM" not in text


class TestNavbar:
    def test_admin_sees_dropdown_menu(self, admin_client):
        html = admin_client.get("/").data.decode()
        assert "Administration" in html
        assert "dropdown-menu" in html
        assert "transcria.css" in html

    def test_viewer_does_not_see_admin_menu(self, viewer_client):
        html = viewer_client.get("/").data.decode()
        assert "Administration" not in html


class TestConfigPrompts:
    def _prompts_tmp(self, monkeypatch, tmp_path):
        cfg = get_config()
        workflow = cfg.setdefault("workflow", {})
        monkeypatch.setitem(workflow, "prompts_dir", str(tmp_path))
        (tmp_path / "summary_prompt.txt").write_text("Prompt résumé v1", encoding="utf-8")
        (tmp_path / "correction_prompt.txt").write_text("Prompt correction v1", encoding="utf-8")
        (tmp_path / "final_review_prompt.txt").write_text("Prompt relecture v1", encoding="utf-8")
        return tmp_path

    def test_config_page_shows_prompts_and_scripts_tabs(self, admin_client, monkeypatch, tmp_path):
        self._prompts_tmp(monkeypatch, tmp_path)
        html = admin_client.get("/admin/config").data.decode()
        assert "Prompts LLM" in html
        assert "Scripts (lecture seule)" in html
        assert "Prompt résumé v1" in html
        assert "lecture seule" in html

    def test_save_prompt_writes_file_and_backup(self, admin_client, monkeypatch, tmp_path):
        base = self._prompts_tmp(monkeypatch, tmp_path)
        resp = admin_client.post("/admin/config", data={
            "_mode": "prompts",
            "prompt-summary_prompt": "Prompt résumé v2 — amélioré",
            "prompt-correction_prompt": "Prompt correction v1",   # inchangé
            "prompt-final_review_prompt": "Prompt relecture v1",  # inchangé
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert (base / "summary_prompt.txt").read_text(encoding="utf-8") == "Prompt résumé v2 — amélioré"
        assert (base / "summary_prompt.txt.bak").read_text(encoding="utf-8") == "Prompt résumé v1"
        # Les prompts non modifiés ne sont pas réécrits (pas de .bak superflu).
        assert not (base / "correction_prompt.txt.bak").exists()

    def test_empty_prompt_is_refused(self, admin_client, monkeypatch, tmp_path):
        base = self._prompts_tmp(monkeypatch, tmp_path)
        resp = admin_client.post("/admin/config", data={
            "_mode": "prompts",
            "prompt-summary_prompt": "   \n  ",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "contenu vide refusé" in resp.data.decode()
        assert (base / "summary_prompt.txt").read_text(encoding="utf-8") == "Prompt résumé v1"

    def test_unknown_prompt_name_is_ignored(self, admin_client, monkeypatch, tmp_path):
        base = self._prompts_tmp(monkeypatch, tmp_path)
        admin_client.post("/admin/config", data={
            "_mode": "prompts",
            "prompt-evil": "../../etc/passwd",
        }, follow_redirects=True)
        # Liste fermée : aucun fichier créé en dehors des 3 prompts connus.
        assert sorted(p.name for p in base.iterdir()) == [
            "correction_prompt.txt", "final_review_prompt.txt", "summary_prompt.txt",
        ]

    def test_prompts_require_manage_config_permission(self, operator_client):
        resp = operator_client.post("/admin/config", data={
            "_mode": "prompts", "prompt-summary_prompt": "pwn",
        })
        assert resp.status_code in (302, 403)  # refusé (redirect login/403 selon RBAC)


class TestSystemPage:
    def test_system_page_shows_role_and_storage(self, admin_client):
        html = admin_client.get("/system").data.decode()
        assert "mode tout-en-un" in html
        assert "Stockage des fichiers de jobs" in html
        assert "backend fs" in html

    def test_system_page_web_role_hides_local_gpu(self, app, admin_client):
        previous = app.config.get("TRANSCRIA_ROLE", "all")
        app.config["TRANSCRIA_ROLE"] = "web"
        try:
            html = admin_client.get("/system").data.decode()
        finally:
            app.config["TRANSCRIA_ROLE"] = previous
        assert "frontale sans GPU" in html
        assert "mode frontale (web)" in html

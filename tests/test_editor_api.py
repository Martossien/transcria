"""Éditeur SRT — API serveur (lot A, docs/EDITEUR_SRT_INTEGRE.md §3.4 + critères §10).

Couvre les contrats : state complet en un appel, brouillon à verrou optimiste (409),
save = snapshot pool commun + recalcul stats (A2) + purge du brouillon + audit,
restauration CROISÉE éditeur↔affinage, stream Range (206), mode sans audio (A1),
pics (404 sans audio / 202 puis 200).
"""
import json
import wave

import pytest
from test_docx_route import _seed_job_files

SRT = (
    "1\n00:00:01,012 --> 00:00:03,910\nSPEAKER_01(Vendeur / fromager): Podcast francefacil.com\n\n"
    "2\n00:00:05,416 --> 00:00:06,762\nSPEAKER_00(Cliente): Fais pas chaud ce matin.\n\n"
    "3\n00:00:07,053 --> 00:00:11,639\nSPEAKER_01(Vendeur / fromager): Rien de bon pour la semaine.\n"
)


@pytest.fixture
def editor_job(admin_client, app):
    """Job COMPLETED avec SRT corrigé (sans audio par défaut — mode dégradé A1)."""
    r = admin_client.post("/jobs/new", data={"title": "Test éditeur"}, follow_redirects=True)
    job_id = r.request.path.split("/")[2]
    from transcria.config import get_config
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore

    with app.app_context():
        cfg = get_config()
        _seed_job_files(cfg["storage"]["jobs_dir"], job_id)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job_id)
        fs.save_text("metadata/transcription_corrigee.srt", SRT)
        JobStore.update_state(job_id, JobState.COMPLETED)
    return job_id


def _add_audio(app, job_id: str) -> None:
    """Ajoute un vrai WAV d'une seconde (silence) comme audio original."""
    from transcria.config import get_config
    from transcria.jobs.filesystem import JobFilesystem

    with app.app_context():
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        path = fs.job_dir / "input" / "original.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 1.0})


class TestState:
    def test_state_complet_en_un_appel(self, admin_client, editor_job):
        r = admin_client.get(f"/api/jobs/{editor_job}/editor/state")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["chunks"]) == 3
        assert data["chunks"][0]["speaker_name"] == "Vendeur / fromager"
        assert data["draft"]["exists"] is False
        assert data["audio"]["available"] is False          # mode dégradé A1
        assert data["readonly"] is False
        assert data["srt_sha256"]

    def test_page_editeur_rendue(self, admin_client, editor_job):
        r = admin_client.get(f"/jobs/{editor_job}/editor")
        html = r.data.decode()
        assert r.status_code == 200
        assert "se-root" in html and "se-list" in html          # atelier + liste
        assert "Enregistrer une version" in html                 # filet n°3 visible
        assert "srt_editor.js" in html

    def test_sans_srt_404(self, admin_client, app):
        r = admin_client.post("/jobs/new", data={"title": "Sans SRT"}, follow_redirects=True)
        job_id = r.request.path.split("/")[2]
        assert admin_client.get(f"/api/jobs/{job_id}/editor/state").status_code == 404
        assert admin_client.get(f"/jobs/{job_id}/editor").status_code == 404

    def test_rbac(self, operator_client, editor_job, client):
        assert operator_client.get(f"/api/jobs/{editor_job}/editor/state").status_code in (403, 404)
        assert client.get(f"/jobs/{editor_job}/editor").status_code in (302, 401)

    def test_readonly_pendant_un_affinage(self, admin_client, app, editor_job):
        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore

        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=editor_job)
            store.write_request(kind="discuss", message="?")
        assert admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()["readonly"] is True


class TestDraft:
    def _chunks(self, admin_client, job_id):
        return admin_client.get(f"/api/jobs/{job_id}/editor/state").get_json()["chunks"]

    def test_verrou_optimiste_409(self, admin_client, editor_job):
        chunks = self._chunks(admin_client, editor_job)
        r1 = admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                              json={"revision": 0, "chunks": chunks})
        assert r1.status_code == 200 and r1.get_json()["revision"] == 1
        # Rejouer avec la révision périmée (autre onglet) → 409 + révision serveur.
        r2 = admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                              json={"revision": 0, "chunks": chunks})
        assert r2.status_code == 409 and r2.get_json()["server_revision"] == 1
        r3 = admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                              json={"revision": 1, "chunks": chunks})
        assert r3.status_code == 200 and r3.get_json()["revision"] == 2

    def test_conflit_detecte_si_srt_change(self, admin_client, app, editor_job):
        chunks = self._chunks(admin_client, editor_job)
        admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                         json={"revision": 0, "chunks": chunks, "base_srt_sha256": "vieux-sha"})
        data = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        assert data["draft"]["exists"] is True and data["draft"]["conflict"] is True

    def test_revision_non_entiere_ne_casse_pas(self, admin_client, editor_job):
        # Chasse aux bugs : une révision non entière côté client ne doit jamais donner
        # un 500 (int('abc') levait ValueError hors du try). Traitée comme 0 (lenient).
        chunks = self._chunks(admin_client, editor_job)
        r = admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                             json={"revision": "pas-un-entier", "chunks": chunks})
        assert r.status_code == 200 and r.get_json()["revision"] == 1

    def test_payload_invalide_400_et_delete(self, admin_client, editor_job):
        assert admin_client.put(f"/api/jobs/{editor_job}/editor/draft",
                                json={"revision": 0, "chunks": "pas une liste"}).status_code == 400
        chunks = self._chunks(admin_client, editor_job)
        admin_client.put(f"/api/jobs/{editor_job}/editor/draft", json={"revision": 0, "chunks": chunks})
        assert admin_client.delete(f"/api/jobs/{editor_job}/editor/draft").status_code == 200
        assert admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()["draft"]["exists"] is False


class TestSave:
    def test_save_ecrit_recalcule_et_purge(self, admin_client, app, editor_job):
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        chunks = state["chunks"]
        chunks[1]["text"] = "Il fait frais ce matin."           # édition texte
        chunks[2]["speaker_id"] = "SPEAKER_07"                  # réattribution → nouveau locuteur
        chunks[2]["speaker_name"] = "Mme X"
        admin_client.put(f"/api/jobs/{editor_job}/editor/draft", json={"revision": 0, "chunks": chunks})

        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save",
                              json={"chunks": chunks, "edited_count": 2,
                                    "new_speakers": [{"speaker_id": "SPEAKER_07", "speaker_name": "Mme X"}]})
        assert r.status_code == 200
        version = r.get_json()["version"]
        assert version >= 1

        from transcria.config import get_config
        from transcria.jobs.filesystem import JobFilesystem
        with app.app_context():
            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], editor_job)
            srt = fs.load_text("metadata/transcription_corrigee.srt")
            stats = fs.load_json("speakers/speaker_stats.json")
            mapping = fs.load_json("speakers/speaker_mapping.json")
            draft_gone = not (fs.job_dir / "metadata" / "srt_editor_draft.json").exists()
        assert "Il fait frais ce matin." in srt
        assert "SPEAKER_07(Mme X):" in srt
        by_id = {s["speaker_id"]: s for s in stats["speakers"]}
        assert by_id["SPEAKER_07"]["turn_count"] == 1           # A2 : stats recalculées
        assert stats["source"] == "srt_editor"
        assert mapping["mapping"]["SPEAKER_07"] == "Mme X"      # A2 : mapping complété
        assert draft_gone                                        # brouillon purgé

    def test_restauration_croisee_pool_commun(self, admin_client, app, editor_job):
        """Une version d'ÉDITEUR se restaure par la route de versions de l'AFFINAGE."""
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        chunks = state["chunks"]
        original_text = chunks[0]["text"]
        chunks[0]["text"] = "Texte modifié par l'éditeur."
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save", json={"chunks": chunks})
        version = r.get_json()["version"]

        r2 = admin_client.post(f"/api/jobs/{editor_job}/refine/revert", json={"version": version})
        assert r2.status_code == 200
        after = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()["chunks"]
        assert after[0]["text"] == original_text

    def test_save_refuse_en_lecture_seule(self, admin_client, app, editor_job):
        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore
        with app.app_context():
            RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=editor_job)\
                .write_request(kind="discuss", message="?")
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save", json={"chunks": state["chunks"]})
        assert r.status_code == 409

    def test_save_forme_invalide_422(self, admin_client, editor_job):
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save",
                              json={"chunks": [{"start_ms": "abc", "end_ms": 1, "text": "x"}]})
        assert r.status_code == 422

    def test_suppression_massive_autorisee(self, admin_client, editor_job):
        """Contrairement à la correction LLM, l'HUMAIN a le droit de tout élaguer."""
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save",
                              json={"chunks": state["chunks"][:1]})
        assert r.status_code == 200


class TestAudioEtPics:
    def test_stream_absent_404_et_pics_404(self, admin_client, editor_job):
        assert admin_client.get(f"/api/jobs/{editor_job}/audio/stream").status_code == 404
        assert admin_client.get(f"/api/jobs/{editor_job}/editor/peaks").status_code == 404

    def test_stream_range_206(self, admin_client, app, editor_job):
        _add_audio(app, editor_job)
        full = admin_client.get(f"/api/jobs/{editor_job}/audio/stream")
        assert full.status_code == 200
        assert "attachment" not in (full.headers.get("Content-Disposition") or "")
        partial = admin_client.get(f"/api/jobs/{editor_job}/audio/stream",
                                   headers={"Range": "bytes=0-999"})
        assert partial.status_code == 206 and len(partial.data) == 1000

    def test_pics_202_puis_200(self, admin_client, app, editor_job):
        import time
        _add_audio(app, editor_job)
        r = admin_client.get(f"/api/jobs/{editor_job}/editor/peaks")
        assert r.status_code in (200, 202)
        deadline = time.time() + 15
        while r.status_code == 202 and time.time() < deadline:
            time.sleep(0.3)
            r = admin_client.get(f"/api/jobs/{editor_job}/editor/peaks")
        assert r.status_code == 200
        meta = json.loads(r.headers["X-Peaks-Meta"])
        assert meta["count"] == len(r.data) and abs(meta["duration_ms"] - 1000) < 100

    def test_state_expose_audio_disponible(self, admin_client, app, editor_job):
        _add_audio(app, editor_job)
        data = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        assert data["audio"]["available"] is True
        assert data["audio"]["duration_ms"] == 1000


class TestSyncSummary:
    """Choix DOCX rapide vs synthèse resynchronisée : suggestion à la sauvegarde,
    enfilage d'une demande refine `apply` composée serveur, gardes anti-double.

    La disponibilité dépend de `workflow.arbitration_llm.enabled` (défaut False —
    c'est le config.yaml local qui l'active en dev) : les tests FORCENT l'état
    voulu pour être déterministes quel que soit l'environnement (leçon CI 0.3.5)."""

    @pytest.fixture(autouse=True)
    def _llm_enabled(self, app):
        from transcria.config import get_config

        with app.app_context():
            cfg = get_config()
        llm = cfg.setdefault("workflow", {}).setdefault("arbitration_llm", {})
        previous = llm.get("enabled")
        llm["enabled"] = True
        yield
        if previous is None:
            llm.pop("enabled", None)
        else:
            llm["enabled"] = previous

    def test_save_suggere_la_mise_a_jour(self, admin_client, editor_job):
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        chunks = state["chunks"]
        chunks[0]["text"] = "Texte corrigé par l'utilisateur."
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save",
                              json={"chunks": chunks, "edited_count": 1, "new_speakers": []})
        assert r.status_code == 200
        body = r.get_json()
        assert body["summary_update_suggested"] is True
        assert body["edited_count"] == 1

    def test_save_sans_modification_ne_suggere_pas(self, admin_client, editor_job):
        state = admin_client.get(f"/api/jobs/{editor_job}/editor/state").get_json()
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/save",
                              json={"chunks": state["chunks"], "edited_count": 0, "new_speakers": []})
        assert r.status_code == 200
        assert r.get_json()["summary_update_suggested"] is False

    def test_sync_enfile_une_demande_apply_composee(self, admin_client, app, editor_job, monkeypatch):
        # C5 : la route importe get_job_executor en tête — patcher le consommateur.
        import transcria.web.editor_routes as editor_mod

        submitted = {}

        class FakeExecutor:
            def submit_process(self, job_id, audio_path, mode):
                submitted.update(job_id=job_id, mode=mode)
                return {"accepted": True}

        monkeypatch.setattr(editor_mod, "get_job_executor", lambda: FakeExecutor())
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/sync-summary",
                              json={"edited_count": 3, "new_speakers_count": 1})
        assert r.status_code == 202, r.get_json()
        assert submitted["job_id"] == editor_job

        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore
        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=editor_job)
            request = store.consume_request()
        assert request["kind"] == "apply"
        assert "3 segment" in request["message"]
        assert "UNIQUEMENT" in request["message"]  # instruction de prudence

    def test_sync_refuse_si_demande_deja_active(self, admin_client, app, editor_job, monkeypatch):
        from transcria.config import get_config
        from transcria.workflow.refine_store import RefineStore
        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=editor_job)
            store.write_request(kind="apply", message="occupé")
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/sync-summary", json={})
        assert r.status_code == 409
        with app.app_context():
            store = RefineStore(jobs_dir=get_config()["storage"]["jobs_dir"], job_id=editor_job)
            store.consume_request()  # nettoyage

    def test_sync_refuse_si_llm_desactivee(self, admin_client, app, editor_job):
        from transcria.config import get_config
        with app.app_context():
            cfg = get_config()
        cfg["workflow"]["arbitration_llm"]["enabled"] = False  # la fixture autouse restaurera
        r = admin_client.post(f"/api/jobs/{editor_job}/editor/sync-summary", json={})
        assert r.status_code == 409

    def test_message_compose_localise(self):
        from transcria.web.editor_routes import _sync_summary_message

        fr = _sync_summary_message("fr", 2, 1)
        en = _sync_summary_message("en", 2, 1)
        assert "2 segment" in fr and "UNIQUEMENT" in fr
        assert "2 segment" in en and "ONLY" in en
        # jamais de contenu réel : instruction abstraite uniquement
        for msg in (fr, en):
            assert "SPEAKER_" not in msg

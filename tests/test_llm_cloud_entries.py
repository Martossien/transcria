"""LLM cloud — refus à la SOUMISSION sur toutes les entrées web (docs/LLM_CLOUD.md § 3.2.3-5).

Entrées couvertes : traitement (process/reprocess), résumé manuel et son autostart, affinage,
synchronisation de synthèse de l'éditeur, ingestion par connecteur ; et le grisage des profils
à LLM pour un propriétaire interdit. Le cloud est activé en patchant ``get_config`` DANS le
module consommateur (jamais à la source). Base PARTAGÉE : utilisateurs, groupes et modèles
(validation par couple fournisseur/modèle) à noms uniques.
"""
from __future__ import annotations

import io
import types
import uuid

import pytest
from builders import make_config
from test_docx_route import _seed_job_files

import transcria.web.editor_routes as editor_routes
import transcria.web.facade_api as facade_api
import transcria.web.processing_api as processing_api
import transcria.web.refine_api as refine_api
import transcria.web.wizard_api as wizard_api
from transcria.auth.api_tokens import create_token
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.llm_cloud.store import LlmCloudStore
from transcria.services.job_service import JobService
from transcria.workflow.profile_availability import CLOUD_FORBIDDEN_REASON, compute_profiles_view

_ROUTE_MODULES = (processing_api, wizard_api, refine_api, editor_routes, facade_api)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _cloud_cfg(model: str, **extra) -> dict:
    """Config cloud complète, stockage des jobs = celui de l'app de test."""
    overrides = {
        "services": {"backend": "cloud"},
        "workflow": {
            "arbitration_llm": {"enabled": True, "model_id": f"mistral/{model}"},
            "summary_llm": {"enabled": True, "model_id": f"mistral/{model}"},
            "summary_autostart": {"enabled": True},
            "refine_chat": {"enabled": True},
        },
    }
    overrides.update(extra)
    return make_config(overrides, jobs_dir=get_config()["storage"]["jobs_dir"])


class _FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit_process(self, job_id, audio_path, mode, **kwargs):
        self.submitted.append((job_id, mode))
        return {"accepted": True, "status": "queued", "mode": mode}


@pytest.fixture
def fake_executor(monkeypatch):
    fake = _FakeExecutor()
    for module in _ROUTE_MODULES:
        monkeypatch.setattr(module, "get_job_executor", lambda: fake)
    return fake


@pytest.fixture
def cloud(monkeypatch):
    """Active le mode cloud (modèle UNIQUE : aucune validation héritée d'un autre test)."""
    model = f"m-{_uid()}"
    cfg = _cloud_cfg(model)
    for module in _ROUTE_MODULES:
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    return types.SimpleNamespace(cfg=cfg, provider="mistral", model=model)


@pytest.fixture
def local(monkeypatch):
    """Mode LOCAL déterministe (défauts du loader, sans ``config.yaml`` machine)."""
    cfg = make_config(jobs_dir=get_config()["storage"]["jobs_dir"])
    for module in _ROUTE_MODULES:
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    return cfg


def _acknowledge(app, cloud, *, connectors_allowed: bool = False) -> None:
    with app.app_context():
        LlmCloudStore.acknowledge(cloud.provider, cloud.model, user_id=None, username="admin-test",
                                  connectors_allowed=connectors_allowed)


@pytest.fixture
def owner(app):
    """Propriétaire NEUF (opérateur) + client connecté."""
    name = f"cloud-{_uid()}"
    with app.app_context():
        user = UserStore.create_user(username=name, password="test12345", role=Role.OPERATOR)
        user_id = str(user.id)
    client = app.test_client()
    client.post("/login", data={"username": name, "password": "test12345"}, follow_redirects=True)
    return types.SimpleNamespace(id=user_id, client=client)


def _forbid(app, user_id: str) -> None:
    with app.app_context():
        group = GroupStore.create_group(f"cloud-interdit-{_uid()}")
        GroupStore.update_group(group.id, group.name, llm_cloud_forbidden=True)
        GroupStore.add_member(group.id, user_id)


def _new_job(app, owner, state: JobState, *, deliverables: bool = False) -> str:
    with app.app_context():
        job_id = JobService.create(owner_id=owner.id, title=f"Cloud {_uid()}")["job_id"]
        jobs_dir = get_config()["storage"]["jobs_dir"]
        JobService.upload(job_id, b"RIFF0000WAVEfmt ", "reunion.wav", jobs_dir)
        if deliverables:
            _seed_job_files(jobs_dir, job_id)
            JobFilesystem(jobs_dir, job_id).save_text(
                "metadata/transcription_corrigee.srt",
                "1\n00:00:00,000 --> 00:00:02,000\nSPEAKER_00: Bonjour.\n")
        JobStore.update_state(job_id, state, force=True)
    return job_id


def _process(owner, job_id: str, profile_id: str):
    return owner.client.post(f"/api/jobs/{job_id}/process", json={"processing_profile_id": profile_id})


# --------------------------------------------------------------------------- #
#  Traitement complet (process / reprocess)
# --------------------------------------------------------------------------- #
class TestProcess:
    def test_sans_validation_profil_LLM_refuse(self, app, owner, cloud, fake_executor):
        job_id = _new_job(app, owner, JobState.READY_TO_PROCESS)
        r = _process(owner, job_id, "word_corrige")
        assert r.status_code == 403
        assert r.get_json()["code"] == "not_acknowledged"
        assert r.get_json()["error"]
        assert fake_executor.submitted == []

    def test_profil_sans_LLM_jamais_refuse_par_la_garde(self, app, owner, cloud, fake_executor):
        job_id = _new_job(app, owner, JobState.READY_TO_PROCESS)
        r = _process(owner, job_id, "srt_express")
        assert (r.get_json() or {}).get("code") is None
        assert r.status_code != 403
        assert fake_executor.submitted == [(job_id, "fast")]

    def test_apres_validation_plus_de_refus(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        job_id = _new_job(app, owner, JobState.READY_TO_PROCESS)
        r = _process(owner, job_id, "word_corrige")
        assert r.status_code == 202, r.get_json()
        assert len(fake_executor.submitted) == 1

    def test_groupe_interdit_refuse_meme_valide(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.READY_TO_PROCESS)
        r = _process(owner, job_id, "word_corrige")
        assert r.status_code == 403
        assert r.get_json()["code"] == "group_forbidden"
        assert fake_executor.submitted == []

    def test_reprocess_groupe_interdit_refuse(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.COMPLETED)
        r = owner.client.post(f"/api/jobs/{job_id}/reprocess", json={"processing_profile_id": "word_corrige"})
        assert r.status_code == 403
        assert r.get_json()["code"] == "group_forbidden"

    def test_local_inchange(self, app, owner, local, fake_executor):
        """Hors cloud : un membre d'un groupe « interdit » traite comme avant."""
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.READY_TO_PROCESS)
        r = _process(owner, job_id, "word_corrige")
        assert r.status_code == 202, r.get_json()


# --------------------------------------------------------------------------- #
#  Résumé manuel + autostart
# --------------------------------------------------------------------------- #
class TestSummary:
    def test_groupe_interdit_refuse(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.status_code == 403
        assert r.get_json()["code"] == "group_forbidden"
        assert fake_executor.submitted == []

    def test_sans_validation_refuse(self, app, owner, cloud, fake_executor):
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.status_code == 403
        assert r.get_json()["code"] == "not_acknowledged"

    def test_resume_sans_LLM_jamais_refuse(self, app, owner, fake_executor, monkeypatch):
        cfg = _cloud_cfg(f"m-{_uid()}", workflow={"summary_llm": {"enabled": False}})
        monkeypatch.setattr(wizard_api, "get_config", lambda: cfg)
        monkeypatch.setitem(app.config, "TRANSCRIA_ROLE", "web")
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.status_code == 200, r.get_json()
        assert r.get_json().get("queued") is True

    def test_local_inchange(self, app, owner, local, fake_executor, monkeypatch):
        _forbid(app, owner.id)
        monkeypatch.setitem(app.config, "TRANSCRIA_ROLE", "web")  # enfile, pas d'exécution synchrone
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.status_code == 200, r.get_json()
        assert fake_executor.submitted == [(job_id, "summary")]


class _SyncThread:
    """Exécute la cible immédiatement (l'autostart tourne en thread en production)."""

    def __init__(self, target, name=None, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


class TestAutostart:
    def _run(self, app, monkeypatch, cfg, job_id: str) -> None:
        def _fake_analyze(jid, jobs_dir, config):
            JobStore.update_state(jid, JobState.ANALYZED)
            return {"ok": True}

        monkeypatch.setattr(wizard_api, "threading", types.SimpleNamespace(Thread=_SyncThread))
        monkeypatch.setattr(wizard_api.JobService, "analyze", staticmethod(_fake_analyze))
        with app.test_request_context():
            wizard_api._maybe_autostart_summary(cfg, job_id)

    def test_refus_cloud_n_enfile_pas_et_journalise(self, app, owner, cloud, fake_executor, monkeypatch, caplog):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.UPLOADED)
        with caplog.at_level("WARNING", logger="transcria.web.wizard_api"):
            self._run(app, monkeypatch, cloud.cfg, job_id)
        assert fake_executor.submitted == []
        assert any("LLM cloud refusée" in rec.getMessage() and "group_forbidden" in rec.getMessage()
                   for rec in caplog.records)

    def test_admis_enfile(self, app, owner, cloud, fake_executor, monkeypatch):
        _acknowledge(app, cloud)
        job_id = _new_job(app, owner, JobState.UPLOADED)
        self._run(app, monkeypatch, cloud.cfg, job_id)
        assert fake_executor.submitted == [(job_id, "summary")]


# --------------------------------------------------------------------------- #
#  Affinage + synchronisation de synthèse
# --------------------------------------------------------------------------- #
class TestRefineEtEditeur:
    @pytest.mark.parametrize("kind", ["discuss", "apply"])
    def test_refine_groupe_interdit(self, app, owner, cloud, fake_executor, kind):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.COMPLETED, deliverables=True)
        r = owner.client.post(f"/api/jobs/{job_id}/refine", json={"kind": kind, "message": "Question ?"})
        assert r.status_code == 403
        assert r.get_json()["code"] == "group_forbidden"
        assert fake_executor.submitted == []

    def test_refine_admis(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        job_id = _new_job(app, owner, JobState.COMPLETED, deliverables=True)
        r = owner.client.post(f"/api/jobs/{job_id}/refine", json={"kind": "discuss", "message": "Question ?"})
        assert r.status_code == 202, r.get_json()

    def test_sync_synthese_groupe_interdit(self, app, owner, cloud, fake_executor):
        _acknowledge(app, cloud)
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.COMPLETED, deliverables=True)
        r = owner.client.post(f"/api/jobs/{job_id}/editor/sync-summary", json={"edited_count": 1})
        assert r.status_code == 403
        assert r.get_json()["code"] == "group_forbidden"
        assert fake_executor.submitted == []

    def test_sync_synthese_sans_validation(self, app, owner, cloud, fake_executor):
        job_id = _new_job(app, owner, JobState.COMPLETED, deliverables=True)
        r = owner.client.post(f"/api/jobs/{job_id}/editor/sync-summary", json={})
        assert r.status_code == 403
        assert r.get_json()["code"] == "not_acknowledged"


# --------------------------------------------------------------------------- #
#  Ingestion par connecteur (/v1/audio/ingest)
# --------------------------------------------------------------------------- #
class TestIngest:
    @pytest.fixture
    def ingest(self, app, monkeypatch, fake_executor):
        model = f"m-{_uid()}"
        cfg = _cloud_cfg(model, live={"facade": {"enabled": True}})
        monkeypatch.setattr(facade_api, "get_config", lambda: cfg)
        monkeypatch.setattr(facade_api, "audit_log", lambda *a, **k: None)
        monkeypatch.setattr(facade_api.JobService, "analyze", staticmethod(lambda *a, **k: {"ok": True}))
        with app.app_context():
            user = UserStore.create_user(f"fac-cloud-{_uid()}", "x" * 24, role=Role.OPERATOR)
            token, _ = create_token(user.id, "cloud-test")
        created: list[str] = []
        real_create = facade_api.JobService.create

        def _create(owner_id, title):
            out = real_create(owner_id=owner_id, title=title)
            created.append(out["job_id"])
            return out

        monkeypatch.setattr(facade_api.JobService, "create", staticmethod(_create))
        return types.SimpleNamespace(cfg=cfg, provider="mistral", model=model, token=token, created=created)

    def _post(self, client, ingest, **form):
        data = {"file": (io.BytesIO(b"RIFF0000WAVEfmt "), "reunion.wav"),
                "processing_profile_id": "srt_express", **form}
        return client.post("/v1/audio/ingest", headers={"Authorization": f"Bearer {ingest.token}"},
                           data=data, content_type="multipart/form-data")

    def test_reunion_sans_connecteurs_autorises_refusee(self, app, client, ingest, fake_executor, caplog):
        _acknowledge(app, ingest, connectors_allowed=False)
        with caplog.at_level("WARNING", logger="transcria.web.llm_cloud_entries"):
            r = self._post(client, ingest, provider="visio", processing_profile_id="word_rapide")
        assert r.status_code == 403, r.get_json()
        assert r.get_json()["code"] == "connectors_not_allowed"
        assert ingest.created == []          # refus AVANT la création : aucun job orphelin
        assert fake_executor.submitted == []
        assert any("connectors_not_allowed" in rec.getMessage() for rec in caplog.records)

    def test_reunion_avec_connecteurs_autorises_passe(self, app, client, ingest, fake_executor):
        _acknowledge(app, ingest, connectors_allowed=True)
        r = self._post(client, ingest, provider="visio")
        assert r.status_code == 202, r.get_json()
        assert len(ingest.created) == 1

    def test_sans_validation_refusee(self, app, client, ingest, fake_executor):
        r = self._post(client, ingest, processing_profile_id="word_rapide")
        assert r.status_code == 403
        assert r.get_json()["code"] == "not_acknowledged"
        assert ingest.created == []

    def test_profil_sans_llm_ingere_meme_sans_validation(self, app, client, ingest, fake_executor):
        # La garde d'ingestion ne juge que le TRAITEMENT du profil : SRT express (sans LLM) reste
        # ingérable pour une réunion même sans validation ni connecteurs autorisés — le résumé
        # autostart, lui, est refusé par sa propre garde (rien n'est enfilé, WARNING).
        r = self._post(client, ingest, provider="visio")
        assert r.status_code == 202, r.get_json()
        assert len(ingest.created) == 1


# --------------------------------------------------------------------------- #
#  Grisage des profils
# --------------------------------------------------------------------------- #
class TestProfilesView:
    def test_cloud_interdit_grise_les_profils_LLM(self):
        cfg = _cloud_cfg(f"m-{_uid()}")
        view = compute_profiles_view(cfg, cloud_forbidden=True)
        by_id = {p["id"]: p for p in view["profiles"]}
        assert by_id["word_corrige"]["status"] == "unavailable"
        assert by_id["word_corrige"]["reasons"] == [CLOUD_FORBIDDEN_REASON]
        assert by_id["srt_express"]["available"] is True
        recommended = by_id[view["recommended"]]
        assert recommended["available"] is True

    def test_raison_traduite(self):
        cfg = _cloud_cfg(f"m-{_uid()}")
        view = compute_profiles_view(cfg, "en", cloud_forbidden=True)
        by_id = {p["id"]: p for p in view["profiles"]}
        assert by_id["word_corrige"]["reasons"] == ["Cloud LLM forbidden for your group"]

    def test_hors_cloud_sans_effet(self):
        cfg = make_config()
        assert compute_profiles_view(cfg, cloud_forbidden=True) == compute_profiles_view(cfg)

    def test_cloud_autorise_inchange(self):
        cfg = _cloud_cfg(f"m-{_uid()}")
        by_id = {p["id"]: p for p in compute_profiles_view(cfg)["profiles"]}
        assert by_id["word_corrige"]["status"] != "unavailable"

    def test_api_disponibilite_grise_pour_un_interdit(self, app, owner, cloud):
        _forbid(app, owner.id)
        r = owner.client.get("/api/profiles/availability")
        by_id = {p["id"]: p for p in r.get_json()["profiles"]}
        assert by_id["word_corrige"]["available"] is False
        assert by_id["srt_express"]["available"] is True

    def test_set_profile_refuse_un_profil_grise(self, app, owner, cloud):
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/profile", json={"processing_profile_id": "word_corrige"})
        assert r.status_code == 409

"""Gouvernance de la LLM d'arbitrage cloud : politique, store, garde, provenance, rempart worker."""
from __future__ import annotations

import json
import uuid

import pytest
from builders import make_config

from transcria.llm_cloud import policy
from transcria.llm_cloud.policy import CLOUD_NOTICE_TEXT_VERSION as V

PROVIDER = "mistral"
MODEL = "mistral-large-4"


def _cloud_config(**kw) -> dict:
    overrides = {
        "services": {"backend": "cloud"},
        "workflow": {
            "arbitration_llm": {"enabled": True, "model_id": f"{PROVIDER}/{MODEL}"},
            "summary_llm": {"enabled": True, "model_id": f"{PROVIDER}/{MODEL}"},
        },
    }
    return make_config(overrides, **kw)


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── policy (pur) ──────────────────────────────────────────────────────────────

def _ack(**over):
    base = dict(ack_provider="mistral", ack_model="m1", ack_text_version=V, ack_revoked=False,
                provider="mistral", model="m1")
    base.update(over)
    return policy.ack_is_valid(**base)


def test_ack_exact_is_valid():
    assert _ack() is True


def test_ack_provider_case_tolerated():
    assert _ack(ack_provider="Mistral", provider="MISTRAL ") is True


def test_ack_model_is_exact():
    assert _ack(model="M1") is False
    assert _ack(model="m2") is False


def test_ack_other_text_version_is_stale():
    assert _ack(ack_text_version="llm-cloud-notice-v0") is False
    assert _ack(ack_text_version=None) is False


def test_ack_revoked_or_empty_is_invalid():
    assert _ack(ack_revoked=True) is False
    assert _ack(ack_provider="") is False
    assert _ack(ack_model=None) is False


def test_user_forbidden_any_group():
    assert policy.user_forbidden([False, True]) is True
    assert policy.user_forbidden([False, False]) is False
    assert policy.user_forbidden([]) is False


def _gate(**over):
    base = dict(cloud_active=True, needs_llm=True, provider_resolved=True, acknowledged=True,
                forbidden=False, from_connector=False, connectors_allowed=False)
    base.update(over)
    return policy.decide_gate(**base)


def test_decide_gate_codes_in_documented_order():
    g = _gate(cloud_active=False, acknowledged=False)
    assert (g.allowed, g.code) == (True, policy.GATE_NOT_CLOUD)
    g = _gate(needs_llm=False, acknowledged=False)
    assert (g.allowed, g.code) == (True, policy.GATE_NO_LLM)
    g = _gate(provider_resolved=False, acknowledged=False, forbidden=True)
    assert (g.allowed, g.code) == (False, policy.GATE_PROVIDER_UNRESOLVED)
    g = _gate(acknowledged=False, forbidden=True)
    assert (g.allowed, g.code) == (False, policy.GATE_NOT_ACKNOWLEDGED)
    g = _gate(forbidden=True, from_connector=True)
    assert (g.allowed, g.code) == (False, policy.GATE_GROUP_FORBIDDEN)
    g = _gate(from_connector=True)
    assert (g.allowed, g.code) == (False, policy.GATE_CONNECTORS_NOT_ALLOWED)
    g = _gate(from_connector=True, connectors_allowed=True)
    assert (g.allowed, g.code) == (True, policy.GATE_OK)
    assert _gate().code == policy.GATE_OK


def test_refused_gates_carry_a_french_message():
    for kw in ({"provider_resolved": False}, {"acknowledged": False}, {"forbidden": True},
               {"from_connector": True}):
        g = _gate(**kw)
        assert g.refused and g.message_fr


def test_job_from_connector():
    assert policy.job_from_connector({"source": "meeting"}) is True
    assert policy.job_from_connector({"source": "upload"}) is False
    assert policy.job_from_connector({}) is False
    assert policy.job_from_connector(None) is False


# ── store ─────────────────────────────────────────────────────────────────────

def _ack_store(app, provider, model, **kw):
    from transcria.llm_cloud.store import LlmCloudStore
    return LlmCloudStore.acknowledge(provider, model, user_id=None, username="adm",
                                     connectors_allowed=kw.pop("connectors_allowed", False), **kw)


def test_store_acknowledge_current_and_supersede(app):
    from transcria.llm_cloud.store import LlmCloudStore

    prov, model = _uniq("prov"), _uniq("model")
    with app.app_context():
        assert LlmCloudStore.current(prov, model) is None
        first = _ack_store(app, prov, model)
        assert LlmCloudStore.current(prov, model).id == first.id
        second = _ack_store(app, prov, model, connectors_allowed=True)
        db_first_id = first.id
        assert LlmCloudStore.current(prov, model).id == second.id
        from transcria.database import db
        from transcria.llm_cloud.models import LlmCloudAcknowledgement
        old = db.session.get(LlmCloudAcknowledgement, db_first_id)
        assert old.revoked_at is not None
        assert second.revoked_at is None


def test_store_is_valid_and_stale_text_version(app):
    from transcria.llm_cloud.store import LlmCloudStore

    prov, model = _uniq("prov"), _uniq("model")
    with app.app_context():
        _ack_store(app, prov, model)
        assert LlmCloudStore.is_valid(prov, model) is True
        assert LlmCloudStore.is_valid(prov, model, text_version="llm-cloud-notice-v999") is False
        assert LlmCloudStore.is_valid(prov, "autre-modele") is False
        assert LlmCloudStore.is_valid("", "") is False


def test_store_revoke(app):
    from transcria.llm_cloud.store import LlmCloudStore

    prov, model = _uniq("prov"), _uniq("model")
    with app.app_context():
        assert LlmCloudStore.revoke(prov, model) is False
        _ack_store(app, prov, model)
        assert LlmCloudStore.revoke(prov, model) is True
        assert LlmCloudStore.is_valid(prov, model) is False
        assert LlmCloudStore.revoke(prov, model) is False


def _user_in_group(forbidden: bool):
    from transcria.auth.groups import GroupStore
    from transcria.auth.models import Role
    from transcria.auth.store import UserStore
    from transcria.database import db

    user = UserStore.create_user(username=_uniq("u"), password="pw", role=Role.OPERATOR)
    group = GroupStore.create_group(_uniq("g"))
    group.llm_cloud_forbidden = forbidden
    db.session.commit()
    GroupStore.add_member(group.id, user.id)
    return user.id


def test_store_user_forbidden(app):
    from transcria.llm_cloud.store import LlmCloudStore

    with app.app_context():
        assert LlmCloudStore.user_forbidden(_user_in_group(True)) is True
        assert LlmCloudStore.user_forbidden(_user_in_group(False)) is False
        assert LlmCloudStore.user_forbidden(None) is False
        assert LlmCloudStore.user_forbidden("inconnu") is False


# ── gate ──────────────────────────────────────────────────────────────────────

def test_cloud_status_not_cloud_no_query(app, monkeypatch):
    from transcria.llm_cloud import gate

    def boom(*a, **k):
        raise AssertionError("aucune requête hors cloud")

    monkeypatch.setattr(gate.LlmCloudStore, "current", boom)
    assert gate.cloud_status(make_config()) == {"active": False}


def test_cloud_status_ack_lifecycle(app):
    from transcria.llm_cloud.gate import cloud_status

    prov, model = _uniq("prov"), _uniq("model")
    cfg = make_config({"services": {"backend": "cloud"},
                       "workflow": {"arbitration_llm": {"model_id": f"{prov}/{model}"},
                                    "summary_llm": {"model_id": f"{prov}/{model}"}}})
    with app.app_context():
        st = cloud_status(cfg)
        assert st["active"] is True and st["acknowledged"] is False
        assert st["connectors_allowed"] is False
        _ack_store(app, prov, model, connectors_allowed=True)
        st = cloud_status(cfg)
        assert st["acknowledged"] is True and st["connectors_allowed"] is True
        assert st["text_version"] == V


def _cfg_for(prov, model, model_id=None):
    mid = model_id if model_id is not None else f"{prov}/{model}"
    return make_config({"services": {"backend": "cloud"},
                        "workflow": {"arbitration_llm": {"model_id": mid},
                                     "summary_llm": {"model_id": mid}}})


def test_check_cloud_gate_codes(app):
    from transcria.llm_cloud.gate import check_cloud_gate
    from transcria.llm_cloud.store import LlmCloudStore

    prov, model = _uniq("prov"), _uniq("model")
    cfg = _cfg_for(prov, model)
    with app.app_context():
        assert check_cloud_gate(make_config(), owner_id=None).code == policy.GATE_NOT_CLOUD
        assert check_cloud_gate(cfg, owner_id=None, needs_llm=False).code == policy.GATE_NO_LLM
        assert check_cloud_gate(cfg, owner_id=None).code == policy.GATE_NOT_ACKNOWLEDGED
        _ack_store(app, prov, model)
        assert check_cloud_gate(cfg, owner_id=None).code == policy.GATE_OK
        forbidden_user = _user_in_group(True)
        assert check_cloud_gate(cfg, owner_id=forbidden_user).code == policy.GATE_GROUP_FORBIDDEN
        meeting = {"source": "meeting"}
        assert check_cloud_gate(cfg, owner_id=None, job_extra_data=meeting).code \
            == policy.GATE_CONNECTORS_NOT_ALLOWED
        _ack_store(app, prov, model, connectors_allowed=True)
        assert check_cloud_gate(cfg, owner_id=None, job_extra_data=meeting).code == policy.GATE_OK
        LlmCloudStore.revoke(prov, model)
        assert check_cloud_gate(cfg, owner_id=None).code == policy.GATE_NOT_ACKNOWLEDGED


def test_check_cloud_gate_provider_unresolved(app):
    from transcria.llm_cloud.gate import check_cloud_gate

    with app.app_context():
        cfg = _cfg_for("x", "y", model_id="sans-barre")
        assert check_cloud_gate(cfg, owner_id=None).code == policy.GATE_PROVIDER_UNRESOLVED


# ── provenance ────────────────────────────────────────────────────────────────

def test_phases_for_mode():
    from transcria.llm_cloud.provenance import phases_for_mode

    assert phases_for_mode("summary", None) == ["summary_llm"]
    assert phases_for_mode("refine", ["correction"]) == ["refine"]
    assert phases_for_mode("speakers", ["correction"]) == []
    assert phases_for_mode("fast", ["transcription", "correction", "export", "final_review"]) \
        == ["correction", "final_review"]
    assert phases_for_mode("quality", ["transcription", "export"]) == []
    assert phases_for_mode("quality", None) == []


def _new_job(owner_id, title="Job cloud"):
    from transcria.jobs.store import JobStore
    return JobStore.create_job(owner_id, title)


def test_record_external_llm_use_writes_file_mirror_and_audit(app, owner_id, tmp_path):
    from transcria.audit.store import AuditStore
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.store import JobStore
    from transcria.llm_cloud.provenance import PROVENANCE_PATH, record_external_llm_use

    cfg = _cloud_config(jobs_dir=str(tmp_path))
    with app.app_context():
        job_id = _new_job(owner_id).id
        data = record_external_llm_use(cfg, job_id, ["correction"])
        assert data["external"] is True and data["provider"] == PROVIDER and data["model"] == MODEL
        saved = JobFilesystem(str(tmp_path), job_id).load_json(PROVENANCE_PATH)
        assert [p["name"] for p in saved["phases"]] == ["correction"]
        record_external_llm_use(cfg, job_id, ["final_review"])
        saved = JobFilesystem(str(tmp_path), job_id).load_json(PROVENANCE_PATH)
        assert [p["name"] for p in saved["phases"]] == ["correction", "final_review"]
        extra = JobStore.get_by_id(job_id).get_extra_data()
        assert extra["llm_provider"] == {"provider": PROVIDER, "model": MODEL, "external": True}
        rows = AuditStore.query(action="job_external_llm", target_id=job_id)
        assert len(rows) == 2
        details = json.loads(rows[0].details_json)
        assert details["owner_id"] == owner_id
        assert details["provider"] == PROVIDER
        assert set(details) == {"provider", "model", "phases", "owner_id"}


def test_record_external_llm_use_noop_outside_cloud_or_without_phases(app, owner_id, tmp_path):
    from transcria.audit.store import AuditStore
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.llm_cloud.provenance import PROVENANCE_PATH, record_external_llm_use

    with app.app_context():
        job_id = _new_job(owner_id).id
        assert record_external_llm_use(make_config(jobs_dir=str(tmp_path)), job_id, ["correction"]) is None
        assert record_external_llm_use(_cloud_config(jobs_dir=str(tmp_path)), job_id, []) is None
        assert JobFilesystem(str(tmp_path), job_id).load_json(PROVENANCE_PATH) is None
        assert AuditStore.query(action="job_external_llm", target_id=job_id) == []


def test_record_external_llm_use_is_best_effort(app, owner_id, tmp_path, monkeypatch):
    from transcria.llm_cloud import provenance

    def boom(*a, **k):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(provenance.JobStore, "update_extra_data", boom)
    with app.app_context():
        job_id = _new_job(owner_id).id
        assert provenance.record_external_llm_use(
            _cloud_config(jobs_dir=str(tmp_path)), job_id, ["correction"]) is None


# ── guard + rempart worker ────────────────────────────────────────────────────

def test_run_needs_llm_by_mode():
    from transcria.services.llm_cloud_guard import run_needs_llm

    cfg = _cloud_config()
    assert run_needs_llm(cfg, None, "summary") is True
    assert run_needs_llm(cfg, None, "refine") is True
    assert run_needs_llm(cfg, None, "speakers") is False
    off = make_config({"workflow": {"summary_llm": {"enabled": False}}})
    assert run_needs_llm(off, None, "summary") is False


def test_run_needs_llm_pipeline_follows_arbitration_flag():
    from builders import make_job_stub

    from transcria.services.llm_cloud_guard import run_needs_llm

    job = make_job_stub()
    assert run_needs_llm(_cloud_config(), job, "fast") is True
    off = _cloud_config()
    off["workflow"]["arbitration_llm"]["enabled"] = False
    assert run_needs_llm(off, job, "fast") is False


def _executor(app, cfg):
    from transcria.services.job_executor import JobExecutorService
    return JobExecutorService(app, cfg)


def _sl():
    from transcria.logging_setup import get_structured_logger
    return get_structured_logger("test")


@pytest.fixture
def exec_cfg(tmp_path):
    cfg = _cloud_config(jobs_dir=str(tmp_path))
    cfg["workflow"]["queue"] = {**cfg["workflow"].get("queue", {}), "enabled": False}
    return cfg


def _pending_job(owner_id):
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore
    from transcria.queue.store import QueueStore

    job = JobStore.create_job(owner_id, "Job refusé")
    JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
    QueueStore.enqueue(job.id, mode="fast")
    return job.id


def test_refuse_pipeline_marks_failed_and_notifies(app, owner_id, exec_cfg, monkeypatch):
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore
    from transcria.queue.store import QueueStore

    calls = []
    monkeypatch.setattr("transcria.services.job_executor._notify",
                        lambda cfg, job, event, error=None: calls.append((event, error)))
    svc = _executor(app, exec_cfg)
    try:
        with app.app_context():
            job_id = _pending_job(owner_id)
            JobStore.update_extra_data(job_id, lambda e: {**e, "workflow_progress": {"message": "x"}})
            job = JobStore.get_by_id(job_id)
            assert svc._refuse_if_cloud_forbidden(job, job_id, "fast", _sl()) is True
            job = JobStore.get_by_id(job_id)
            assert job.state == JobState.FAILED.value
            assert "workflow_progress" not in job.get_extra_data()
            assert QueueStore.get_entry(job_id).status == "failed"
            assert calls and calls[0][0] == "failed" and "not_acknowledged" in calls[0][1]
    finally:
        svc._executor.shutdown(wait=False)


def test_refuse_summary_sets_flag_without_state_change(app, owner_id, exec_cfg, monkeypatch):
    from transcria.jobs.store import JobStore

    calls = []
    monkeypatch.setattr("transcria.services.job_executor._notify",
                        lambda *a, **k: calls.append(a))
    svc = _executor(app, exec_cfg)
    try:
        with app.app_context():
            job_id = _pending_job(owner_id)
            state_before = JobStore.get_by_id(job_id).state
            job = JobStore.get_by_id(job_id)
            assert svc._refuse_if_cloud_forbidden(job, job_id, "summary", _sl()) is True
            after = JobStore.get_by_id(job_id)
            extra = after.get_extra_data()
            assert extra["summary_llm_failed"] is True
            assert extra["summary_llm_error_kind"] == "cloud_refused"
            assert after.state == state_before
            assert calls == []
    finally:
        svc._executor.shutdown(wait=False)


def test_refuse_refine_consumes_request_and_explains(app, owner_id, exec_cfg):
    from transcria.jobs.store import JobStore
    from transcria.workflow.refine_store import RefineStore

    svc = _executor(app, exec_cfg)
    try:
        with app.app_context():
            job_id = _pending_job(owner_id)
            store = RefineStore(jobs_dir=exec_cfg["storage"]["jobs_dir"], job_id=job_id)
            store.write_request(kind="discuss", message="Peux-tu préciser ?")
            assert store.has_active_request()
            job = JobStore.get_by_id(job_id)
            assert svc._refuse_if_cloud_forbidden(job, job_id, "refine", _sl()) is True
            assert not store.has_active_request()
            last = store.load_turns()[-1]
            assert last["role"] == "assistant"
            assert "LLM cloud refusée" in last["text"]
    finally:
        svc._executor.shutdown(wait=False)


def test_refuse_allowed_touches_nothing(app, owner_id, exec_cfg):
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore
    from transcria.queue.store import QueueStore

    prov, model = _uniq("prov"), _uniq("model")
    cfg = _cfg_for(prov, model)
    cfg["storage"]["jobs_dir"] = exec_cfg["storage"]["jobs_dir"]
    cfg["workflow"]["queue"] = {"enabled": False}
    svc = _executor(app, cfg)
    try:
        with app.app_context():
            _ack_store(app, prov, model)
            job_id = _pending_job(owner_id)
            job = JobStore.get_by_id(job_id)
            assert svc._refuse_if_cloud_forbidden(job, job_id, "fast", _sl()) is False
            assert JobStore.get_by_id(job_id).state == JobState.READY_TO_PROCESS.value
            assert QueueStore.get_entry(job_id).status != "failed"
    finally:
        svc._executor.shutdown(wait=False)


def test_refuse_outside_cloud_makes_no_query(app, owner_id, tmp_path, monkeypatch):
    from transcria.jobs.store import JobStore
    from transcria.llm_cloud import gate

    def boom(*a, **k):
        raise AssertionError("aucune requête hors cloud")

    monkeypatch.setattr(gate.LlmCloudStore, "current", boom)
    monkeypatch.setattr(gate.LlmCloudStore, "user_forbidden", boom)
    cfg = make_config(jobs_dir=str(tmp_path))
    cfg["workflow"]["queue"] = {"enabled": False}
    svc = _executor(app, cfg)
    try:
        with app.app_context():
            job_id = _pending_job(owner_id)
            job = JobStore.get_by_id(job_id)
            assert svc._refuse_if_cloud_forbidden(job, job_id, "fast", _sl()) is False
    finally:
        svc._executor.shutdown(wait=False)


def test_record_cloud_provenance_pipeline_uses_completed_phases(app, owner_id, tmp_path):
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.store import JobStore
    from transcria.llm_cloud.provenance import PROVENANCE_PATH
    from transcria.services.llm_cloud_guard import record_cloud_provenance

    cfg = _cloud_config(jobs_dir=str(tmp_path))
    with app.app_context():
        job_id = _new_job(owner_id).id
        JobStore.update_extra_data(
            job_id, lambda e: {**e, "pipeline": {"completed_phases": ["transcription", "correction"]}})
        record_cloud_provenance(cfg, job_id, "fast")
        saved = JobFilesystem(str(tmp_path), job_id).load_json(PROVENANCE_PATH)
        # multi_stt est ACTIF par défaut dans le loader : ses appels directs précèdent la
        # correction et sont tracés dès que la correction a eu lieu (relecture lot B, I3).
        assert [p["name"] for p in saved["phases"]] == ["multi_stt_review", "correction"]
        assert all(p["provider"] == "mistral" for p in saved["phases"])
        assert saved["providers"] == ["mistral"]
        other = _new_job(owner_id).id
        record_cloud_provenance(cfg, other, "speakers")
        assert JobFilesystem(str(tmp_path), other).load_json(PROVENANCE_PATH) is None

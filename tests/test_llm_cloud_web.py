"""Écran d'administration LLM cloud, bandeaux et interdiction par groupe (LLM_CLOUD § 3.2)."""
import re
import uuid

import pytest
from builders import make_config

from transcria.audit.models import AuditAction, AuditLog
from transcria.auth.models import Group, Role
from transcria.auth.store import UserStore
from transcria.database import db
from transcria.llm_cloud.store import LlmCloudStore
from transcria.web import llm_cloud_routes
from transcria.web import routes as web_routes


def _cloud_cfg(model: str) -> dict:
    ref = f"mistral/{model}"
    return make_config({
        "services": {"backend": "cloud"},
        "workflow": {"arbitration_llm": {"enabled": True, "model_id": ref},
                     "summary_llm": {"enabled": True, "model_id": ref}},
    })


@pytest.fixture
def cloud(monkeypatch):
    """Active le cloud vu par l'écran ET par le context processor ; modèle unique par test
    (base partagée entre modules : aucune validation héritée)."""
    model = f"modele-test-{uuid.uuid4().hex[:8]}"
    cfg = _cloud_cfg(model)
    monkeypatch.setattr(llm_cloud_routes, "get_config", lambda: cfg)
    monkeypatch.setattr(web_routes, "get_config", lambda: cfg)
    return model


@pytest.fixture
def local_cfg(monkeypatch):
    cfg = make_config({"services": {"backend": "script"}})
    monkeypatch.setattr(llm_cloud_routes, "get_config", lambda: cfg)
    monkeypatch.setattr(web_routes, "get_config", lambda: cfg)
    return cfg


def _audit_rows(app, action: AuditAction, model: str) -> list:
    with app.app_context():
        rows = db.session.execute(db.select(AuditLog).filter_by(action=action.value)).scalars().all()
        return [r for r in rows if model in (r.details_json or "")]


def test_screen_admin_shows_facts_scope_and_unacknowledged(admin_client, cloud):
    resp = admin_client.get("/admin/llm-cloud")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert cloud in html
    assert "data-llm-cloud-facts" in html
    assert "data-llm-cloud-scope" in html
    assert "Jamais l&#39;audio ni les voix" in html or "Jamais l'audio ni les voix" in html
    assert "redémarrage du service" in html
    assert "data-llm-cloud-unacknowledged" in html
    assert "Retirer la validation" not in html


def test_screen_forbidden_for_operator(operator_client, cloud):
    assert operator_client.get("/admin/llm-cloud").status_code == 403
    assert operator_client.post("/admin/llm-cloud/acknowledge").status_code == 403


def test_acknowledge_then_revoke(app, admin_client, cloud):
    resp = admin_client.post("/admin/llm-cloud/acknowledge", data={"connectors_allowed": "1"})
    assert resp.status_code == 302
    with app.app_context():
        ack = LlmCloudStore.current("mistral", cloud)
        assert ack is not None and ack.connectors_allowed is True
        assert ack.acknowledged_by_username == "admin"
        assert LlmCloudStore.is_valid("mistral", cloud)
    rows = _audit_rows(app, AuditAction.CONFIG_LLM_CLOUD_ACKNOWLEDGE, cloud)
    assert len(rows) == 1
    assert '"connectors_allowed": true' in rows[0].details_json
    assert "key" not in rows[0].details_json.lower()

    html = admin_client.get("/admin/llm-cloud").get_data(as_text=True)
    assert "data-llm-cloud-ack" in html and "admin" in html
    assert "Retirer la validation" in html
    assert re.search(r'id="llm-cloud-connectors"\s+checked', html)

    resp = admin_client.post("/admin/llm-cloud/revoke")
    assert resp.status_code == 302
    with app.app_context():
        assert not LlmCloudStore.is_valid("mistral", cloud)
    assert len(_audit_rows(app, AuditAction.CONFIG_LLM_CLOUD_REVOKE, cloud)) == 1


def test_acknowledge_without_connectors_checkbox(app, admin_client, cloud):
    admin_client.post("/admin/llm-cloud/acknowledge", data={})
    with app.app_context():
        ack = LlmCloudStore.current("mistral", cloud)
        assert ack is not None and ack.connectors_allowed is False


def test_inactive_screen_explains_and_refuses(admin_client, local_cfg):
    resp = admin_client.get("/admin/llm-cloud")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "data-llm-cloud-inactive" in html
    assert "redémarrez le service" in html
    assert admin_client.post("/admin/llm-cloud/acknowledge", data={"connectors_allowed": "1"}).status_code == 400
    assert admin_client.post("/admin/llm-cloud/revoke").status_code == 400


def test_banner_on_home_in_cloud(admin_client, cloud):
    html = admin_client.get("/").get_data(as_text=True)
    assert "data-llm-cloud-banner" in html
    assert "Cette instance confie les transcriptions à" in html
    assert "non encore validée" in html
    assert "/admin/llm-cloud" in html


def test_banner_acknowledged_variant(admin_client, cloud):
    admin_client.post("/admin/llm-cloud/acknowledge", data={})
    html = admin_client.get("/").get_data(as_text=True)
    assert "data-llm-cloud-banner" in html
    assert "non encore validée" not in html


def test_no_banner_outside_cloud(admin_client, local_cfg):
    html = admin_client.get("/").get_data(as_text=True)
    assert "data-llm-cloud-banner" not in html


def test_group_llm_cloud_forbidden_toggle(app, admin_client):
    suffix = uuid.uuid4().hex[:8]
    with app.app_context():
        member = UserStore.create_user(username=f"cloudmember-{suffix}", password="motdepasse-1",
                                       display_name="Membre", role=Role.OPERATOR)
        member_id = member.id
    resp = admin_client.post("/admin/groups/new", data={"name": f"grp-cloud-{suffix}", "description": "",
                                                        "llm_cloud_forbidden": "1"})
    assert resp.status_code == 302
    with app.app_context():
        group = db.session.execute(db.select(Group).filter_by(name=f"grp-cloud-{suffix}")).scalar_one()
        group_id = group.id
        assert group.llm_cloud_forbidden is True
    admin_client.post(f"/admin/groups/{group_id}/edit", data={"action": "add_member", "user_id": member_id,
                                                               "role": "member"})
    with app.app_context():
        assert LlmCloudStore.user_forbidden(member_id) is True

    html = admin_client.get("/admin/groups").get_data(as_text=True)
    assert "LLM cloud interdite" in html

    admin_client.post(f"/admin/groups/{group_id}/edit", data={"action": "update_group",
                                                               "name": f"grp-cloud-{suffix}", "description": ""})
    with app.app_context():
        assert db.session.get(Group, group_id).llm_cloud_forbidden is False
        assert LlmCloudStore.user_forbidden(member_id) is False

    admin_client.post(f"/admin/groups/{group_id}/edit", data={"action": "update_group",
                                                               "name": f"grp-cloud-{suffix}", "description": "",
                                                               "llm_cloud_forbidden": "1"})
    with app.app_context():
        assert db.session.get(Group, group_id).llm_cloud_forbidden is True
        assert LlmCloudStore.user_forbidden(member_id) is True


def test_stale_acknowledgement_is_shown(app, admin_client, cloud):
    with app.app_context():
        LlmCloudStore.acknowledge("mistral", cloud, user_id=None, username="ancien",
                                  connectors_allowed=False, text_version="texte-ancien")
    html = admin_client.get("/admin/llm-cloud").get_data(as_text=True)
    assert "data-llm-cloud-stale" in html
    assert "CADUQUE" in html
    assert "Retirer la validation" in html
    assert not re.search(r'id="llm-cloud-connectors"\s+checked', html)

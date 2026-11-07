"""Routes « Discuter avec mes réunions » et « Mes actions » (docs/DISCUTER_REUNIONS.md § 3, lot C).

Le web écrit et lit ; il n'appelle jamais la LLM. Noms = substituts neutres.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from builders import make_job

from transcria.ask_meetings import actions as actions_mod
from transcria.ask_meetings.models import STATUS_WAITING, AskRequest, AskTurn, AskTurnCitation
from transcria.ask_meetings.store import AskStore
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.web import ask_routes

CONTEXT = "context/meeting_context.json"


@pytest.fixture
def clean(app):
    with app.app_context():
        db.session.execute(db.delete(AskTurnCitation))
        db.session.execute(db.delete(AskTurn))
        db.session.execute(db.delete(AskRequest))
        db.session.commit()
    yield


def _login(app, username: str, password: str):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
    return client


@pytest.fixture
def user_client(app, clean):
    with app.app_context():
        name = f"askweb_{uuid.uuid4().hex[:8]}"
        user = UserStore.create_user(username=name, password="mdp-de-test-web", role=Role.OPERATOR)
        user_id = user.id
    client = _login(app, name, "mdp-de-test-web")
    return SimpleNamespace(client=client, user_id=user_id, username=name)


def _meeting(app, owner_id: str, *, title: str, actions=(), meeting_type="Réunion interne", sensitivity="normal",
             when="2026-05-12"):
    with app.app_context():
        job = make_job(owner_id, title=title)
        jobs_dir = get_config()["storage"]["jobs_dir"]
        JobFilesystem(jobs_dir, job.id).save_json(CONTEXT, {
            "title": title, "meeting_type": meeting_type, "sensitivity": sensitivity, "date": when,
            "structured_data": {"actions": list(actions)},
        })
        return job.id


# ── Page et soumission ───────────────────────────────────────────────────────

def test_page_discuter_rend_le_formulaire(user_client):
    resp = user_client.client.get("/meetings/ask")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="ask-form"' in html and 'id="ask-settings"' in html
    assert "js/ask_meetings_page.js" in html
    assert "<script>" not in html.split('id="ask-settings"')[1].split("</script>")[0]   # îlot JSON, pas de JS inline


def test_page_discuter_desactivee(user_client, monkeypatch):
    cfg = get_config()
    monkeypatch.setitem(cfg.setdefault("workflow", {}), "ask_meetings", {"enabled": False})
    html = user_client.client.get("/meetings/ask").get_data(as_text=True)
    assert "data-ask-disabled" in html and 'id="ask-form"' not in html
    resp = user_client.client.post("/api/meetings/ask", json={"question": "q"})
    assert resp.status_code == 403


@pytest.mark.parametrize(("payload", "code"), [
    ({"question": ""}, "empty"),
    ({"question": "   "}, "empty"),
    ({}, "empty"),
    ({"question": "x" * 2001}, "too_long"),
    ({"question": "q", "level": 2}, "level"),
    ({"question": "q", "level": 3}, "target"),
    ({"question": "q", "level": 3, "target_job_id": "../x"}, "target"),
    ({"question": "q", "conversation_id": "pas-un-uuid"}, "conversation"),
])
def test_soumission_refusee_sans_500_message_qui_guide(user_client, payload, code):
    resp = user_client.client.post("/api/meetings/ask", json=payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == code and body["error"]


def test_corps_non_objet_refuse_proprement(user_client):
    resp = user_client.client.post("/api/meetings/ask", json="une chaîne")
    assert resp.status_code == 400


def test_soumission_ecrit_la_demande_et_previent_l_executeur(app, user_client, monkeypatch):
    submitted = []
    monkeypatch.setattr(ask_routes, "get_job_executor",
                        lambda: SimpleNamespace(submit_ask=lambda rid: submitted.append(rid) or {"accepted": True}))
    resp = user_client.client.post("/api/meetings/ask", json={
        "question": "  会議 : qu'a-t-on décidé ? 🎯 ", "include_confidential": True})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == STATUS_WAITING and uuid.UUID(body["conversation_id"])
    assert submitted == [body["request_id"]]
    with app.app_context():
        req = AskStore.get(body["request_id"])
        assert req.user_id == user_client.user_id and req.question == "会議 : qu'a-t-on décidé ? 🎯"
        assert req.include_confidential is True and req.locale == "fr"


def test_soumission_sans_executeur_reste_en_attente(app, user_client, monkeypatch):
    monkeypatch.setattr(ask_routes, "get_job_executor", lambda: None)
    resp = user_client.client.post("/api/meetings/ask", json={"question": "q"})
    assert resp.status_code == 202


def test_refus_cloud_a_la_soumission(user_client, monkeypatch):
    monkeypatch.setattr(ask_routes, "check_cloud_gate",
                        lambda cfg, *, owner_id, needs_llm: SimpleNamespace(refused=True, code="forbidden", message_fr="Groupe interdit"))
    resp = user_client.client.post("/api/meetings/ask", json={"question": "q"})
    assert resp.status_code == 403 and resp.get_json()["error"] == "Groupe interdit"
    html = user_client.client.get("/meetings/ask").get_data(as_text=True)
    assert "data-ask-cloud-refused" in html and 'id="ask-form"' not in html


# ── Interrogation ────────────────────────────────────────────────────────────

def test_etat_d_une_demande_visible_par_son_auteur_seulement(app, user_client):
    other_name = f"askother_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        UserStore.create_user(username=other_name, password="mdp-de-test-web", role=Role.OPERATOR)
    other = _login(app, other_name, "mdp-de-test-web")
    mine = _meeting(app, user_client.user_id, title="Ma réunion budget")
    with app.app_context():
        req = AskStore.create_request(user_id=user_client.user_id, question="budget ?")
        req_id = req.id
        AskStore.claim_next()
        AskStore.complete(req_id, answer="Validé [1]", cited_job_ids=[mine, str(uuid.uuid4())], level_reached=2)

    resp = user_client.client.get(f"/api/meetings/ask/{req_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "done" and body["answer"] == "Validé [1]"
    assert body["level_reached"] == 2 and body["level_label"] == "d'après la synthèse des réunions les plus proches"
    assert [c["job_id"] for c in body["cited"]] == [mine]            # l'identifiant inconnu n'est pas détaillé
    assert body["cited"][0]["url"] == f"/jobs/{mine}/result" and body["cited"][0]["title"] == "Ma réunion budget"
    assert body["cited"][0]["date"] == "2026-05-12"

    assert other.get(f"/api/meetings/ask/{req_id}").status_code == 404
    assert user_client.client.get(f"/api/meetings/ask/{uuid.uuid4()}").status_code == 404


def test_etat_en_attente_et_en_echec(app, user_client):
    with app.app_context():
        waiting = AskStore.create_request(user_id=user_client.user_id, question="q")
        waiting_id = waiting.id
        failed = AskStore.create_request(user_id=user_client.user_id, question="q2")
        failed_id = failed.id
    body = user_client.client.get(f"/api/meetings/ask/{waiting_id}").get_json()
    assert body["status"] == "waiting" and body["answer"] == "" and body["cited"] == []
    with app.app_context():
        AskStore.claim_next()
        AskStore.claim_next()
        AskStore.fail(failed_id, "Aucune réunion ne vous est visible.")
    body = user_client.client.get(f"/api/meetings/ask/{failed_id}").get_json()
    assert body["status"] == "failed" and "Aucune réunion" in body["error"]


def test_reunion_citee_devenue_invisible_n_est_pas_detaillee(app, user_client):
    stranger_name = f"askstr_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        stranger = UserStore.create_user(username=stranger_name, password="mdp-de-test-web", role=Role.OPERATOR)
        stranger_id = stranger.id
    hidden = _meeting(app, stranger_id, title="Pas à moi")
    with app.app_context():
        req = AskStore.create_request(user_id=user_client.user_id, question="q")
        req_id = req.id
        AskStore.claim_next()
        AskStore.complete(req_id, answer="x [1]", cited_job_ids=[hidden], level_reached=1)
    body = user_client.client.get(f"/api/meetings/ask/{req_id}").get_json()
    assert body["cited"] == []


def test_historique_d_une_conversation(app, user_client):
    mine = _meeting(app, user_client.user_id, title="Citée")
    conv = str(uuid.uuid4())
    with app.app_context():
        AskStore.append_turn(conversation_id=conv, user_id=user_client.user_id, role="user", text="q1")
        AskStore.append_turn(conversation_id=conv, user_id=user_client.user_id, role="assistant", text="r1 [1]",
                             level=1, cited_job_ids=[mine])
    body = user_client.client.get(f"/api/meetings/ask/history?conversation_id={conv.upper()}").get_json()
    assert [t["role"] for t in body["turns"]] == ["user", "assistant"]
    assert body["turns"][1]["cited"][0]["job_id"] == mine and body["turns"][1]["level_label"]
    assert user_client.client.get("/api/meetings/ask/history").get_json() == {"turns": []}
    assert user_client.client.get(f"/api/meetings/ask/history?conversation_id={uuid.uuid4()}").get_json() == {"turns": [], "pending": []}


def test_acces_anonyme_refuse(app):
    anon = app.test_client()
    assert anon.get("/meetings/ask").status_code in (302, 401)
    assert anon.post("/api/meetings/ask", json={"question": "q"}).status_code in (302, 401)
    assert anon.get("/meetings/actions").status_code in (302, 401)


# ── Mes actions ──────────────────────────────────────────────────────────────

def test_mes_actions_par_reunion_puis_par_responsable(app, user_client):
    older = _meeting(app, user_client.user_id, title="Réunion ancienne", when="2026-01-10",
                     actions=["Personne A : envoyer le devis (vendredi)", "faire le point"])
    newer = _meeting(app, user_client.user_id, title="Réunion récente", when="2026-03-04", meeting_type="Réunion projet",
                     actions=["personne a : relancer", "Réunion à 14:00 : prévoir la salle"])
    _meeting(app, user_client.user_id, title="Secrète", sensitivity="high", actions=["Personne B : chut"])

    html = user_client.client.get("/meetings/actions").get_data(as_text=True)
    assert html.index("Réunion récente") < html.index("Réunion ancienne")         # la plus récente d'abord
    assert "4 actions" in html and "1 réunion confidentielle exclue" in html
    assert "chut" not in html
    assert f"/jobs/{older}/result" in html and f"/jobs/{newer}/result" in html
    assert "<strong>Personne A</strong> : envoyer le devis (vendredi)" in html

    html = user_client.client.get("/meetings/actions?by=owner").get_data(as_text=True)
    groups = html.split("data-actions-group")[1:]
    assert len(groups) == 2                                                         # Personne A (2 graphies) + non attribué
    assert "Personne A" in groups[0] and "relancer" in groups[0] and "envoyer le devis" in groups[0]
    assert "non attribué" in groups[1] and "prévoir la salle" in groups[1]       # « 14:00 » n'est pas un responsable
    assert "faire le point" in groups[1]

    html = user_client.client.get("/meetings/actions?confidential=1").get_data(as_text=True)
    assert "chut" in html and "5 actions" in html

    html = user_client.client.get("/meetings/actions?type=Réunion+projet&from=2026-02-01&to=2026-12-31").get_data(as_text=True)
    assert "Réunion récente" in html and "Réunion ancienne" not in html and "2 actions" in html

    html = user_client.client.get("/meetings/actions?from=2027-01-01").get_data(as_text=True)
    assert "data-actions-empty" in html
    assert user_client.client.get("/meetings/actions?from=pas-une-date&by=zzz").status_code == 200


def test_mes_actions_ne_montre_pas_les_reunions_des_autres(app, user_client):
    stranger_name = f"askstr_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        stranger = UserStore.create_user(username=stranger_name, password="mdp-de-test-web", role=Role.OPERATOR)
        stranger_id = stranger.id
    _meeting(app, stranger_id, title="D'un inconnu", actions=["Personne X : secret"])
    html = user_client.client.get("/meetings/actions?confidential=1").get_data(as_text=True)
    assert "secret" not in html and "D'un inconnu" not in html


# ── Helper pur ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("Personne A : envoyer le devis (vendredi)", ("Personne A", "envoyer le devis (vendredi)")),
    ("Service RH: relancer", ("Service RH", "relancer")),
    ("Réunion à 14:00 : prévoir la salle", ("", "Réunion à 14:00 : prévoir la salle")),
    ("faire le point", ("", "faire le point")),
    ("", ("", "")),
    ("9h30 : café", ("", "9h30 : café")),
    ("A" * 200 + " : trop long", ("", "A" * 200 + " : trop long")),
])
def test_responsable_avant_le_premier_deux_points_suivi_d_un_espace(raw, expected):
    assert actions_mod.split_owner(raw) == expected


def test_groupes_purs():
    card = SimpleNamespace(meeting_type="T", meeting_date=date(2026, 1, 1), date_is_import_fallback=False,
                           title="R", actions=["Éric : a", "eric : b", "sans"])
    cand = SimpleNamespace(card=card, job=SimpleNamespace(id="j", title="R"))
    lines = actions_mod.action_lines([cand])
    by_owner = actions_mod.group_by_owner(lines)
    assert [g.label for g in by_owner] == ["Éric", ""] and len(by_owner[0].lines) == 2     # accents/casse regroupés
    assert actions_mod.group_by_meeting(lines)[0].key == "j"
    assert actions_mod.meeting_types_present([cand]) == ["T"]


# ── Relecture 2026-11-06 ─────────────────────────────────────────────────────

def test_un_autre_utilisateur_avec_le_meme_conversation_id_ne_lit_rien(app, user_client):
    other_name = f"askother_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        UserStore.create_user(username=other_name, password="mdp-de-test-web", role=Role.OPERATOR)
    conv = str(uuid.uuid4())
    with app.app_context():
        AskStore.append_turn(conversation_id=conv, user_id=user_client.user_id, role="user", text="ma question")
    other = _login(app, other_name, "mdp-de-test-web")
    assert other.get(f"/api/meetings/ask/history?conversation_id={conv}").get_json() == {"turns": [], "pending": []}


def test_include_confidential_est_un_booleen_strict(app, user_client, monkeypatch):
    monkeypatch.setattr(ask_routes, "get_job_executor", lambda: None)
    for raw, expected in (("false", False), (False, False), (0, False), ("true", True), (True, True), (1, True)):
        body = user_client.client.post("/api/meetings/ask", json={"question": "q", "include_confidential": raw}).get_json()
        with app.app_context():
            assert AskStore.get(body["request_id"]).include_confidential is expected, raw
    assert user_client.client.post("/api/meetings/ask", json={"question": {"a": 1}}).status_code == 400


def test_demande_jamais_traitee_expire_a_la_lecture_dans_la_langue(app, user_client):
    """Tier ``web`` sans ordonnanceur : personne n'expire la demande — la lecture le fait, et le
    message est traduit, pas une clé brute."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    with app.app_context():
        req = AskStore.create_request(user_id=user_client.user_id, question="q", locale="en")
        req_id = req.id
        db.session.execute(text("UPDATE ask_requests SET created_at=:t WHERE id=:id"),
                           {"t": datetime.now(UTC) - timedelta(hours=1), "id": req_id})
        db.session.commit()
    body = user_client.client.get(f"/api/meetings/ask/{req_id}").get_json()
    assert body["status"] == "failed" and body["error"] == "The request expired before being processed (LLM busy for too long)."
    with app.app_context():
        fresh = AskStore.create_request(user_id=user_client.user_id, question="q")
        fresh_id = fresh.id
    assert user_client.client.get(f"/api/meetings/ask/{fresh_id}").get_json()["status"] == "waiting"


def test_mes_actions_ignore_les_exclusions_cloud(app, user_client, monkeypatch):
    """La page n'appelle aucune LLM : une réunion captée par connecteur, ou d'un propriétaire
    interdit de cloud, reste listée (ces exclusions protègent ce qui PART, pas ce qu'on lit)."""
    from transcria.ask_meetings import candidates as cand_mod
    from transcria.jobs.store import JobStore

    job_id = _meeting(app, user_client.user_id, title="Captée", actions=["Personne A : relancer"])
    with app.app_context():
        JobStore.update_extra_data(job_id, lambda e: {**e, "source": "meeting"})
    monkeypatch.setattr(cand_mod, "is_cloud_backend", lambda config: True)
    monkeypatch.setattr(cand_mod.LlmCloudView, "from_config",
                        classmethod(lambda cls, config: SimpleNamespace(provider="p", model="m")))
    monkeypatch.setattr(cand_mod.LlmCloudStore, "current",
                        staticmethod(lambda provider, model: SimpleNamespace(connectors_allowed=False)))
    monkeypatch.setattr(cand_mod.LlmCloudStore, "user_forbidden", staticmethod(lambda uid: True))
    html = user_client.client.get("/meetings/actions").get_data(as_text=True)
    assert "relancer" in html


def test_carte_citee_porte_le_drapeau_confidentiel(app, user_client):
    secret = _meeting(app, user_client.user_id, title="Secrète", sensitivity="high")
    with app.app_context():
        req = AskStore.create_request(user_id=user_client.user_id, question="q", include_confidential=True)
        req_id = req.id
        AskStore.claim_next()
        AskStore.complete(req_id, answer="x [1]", cited_job_ids=[secret], level_reached=1)
    body = user_client.client.get(f"/api/meetings/ask/{req_id}").get_json()
    assert body["cited"][0]["confidential"] is True


def test_historique_rend_les_demandes_en_cours_et_la_liste_des_conversations(app, user_client):
    """Parti avant la réponse : la page retrouve la demande en cours et reprend l'interrogation ;
    les conversations récentes sont listées, celles d'un autre utilisateur jamais."""
    conv = str(uuid.uuid4())
    with app.app_context():
        AskStore.append_turn(conversation_id=conv, user_id=user_client.user_id, role="user", text="première question")
        AskStore.append_turn(conversation_id=conv, user_id=user_client.user_id, role="assistant", text="r1", level=1)
        pending = AskStore.create_request(user_id=user_client.user_id, question="encore en cours", conversation_id=conv)
        pending_id = pending.id
        other_conv = str(uuid.uuid4())
        AskStore.append_turn(conversation_id=other_conv, user_id=user_client.user_id, role="user", text="x" * 300)
    body = user_client.client.get(f"/api/meetings/ask/history?conversation_id={conv}").get_json()
    assert [t["text"] for t in body["turns"]] == ["première question", "r1"]
    assert body["pending"] == [{"request_id": pending_id, "question": "encore en cours"}]

    convs = user_client.client.get("/api/meetings/ask/conversations").get_json()["conversations"]
    assert [c["conversation_id"] for c in convs][:2] == [other_conv, conv]        # la plus récente d'abord
    assert convs[1]["first_question"] == "première question" and convs[1]["turns"] == 2
    assert len(convs[0]["first_question"]) == 118 and convs[0]["first_question"].endswith("…")

    other_name = f"askother_{uuid.uuid4().hex[:8]}"
    with app.app_context():
        UserStore.create_user(username=other_name, password="mdp-de-test-web", role=Role.OPERATOR)
    other = _login(app, other_name, "mdp-de-test-web")
    assert other.get("/api/meetings/ask/conversations").get_json() == {"conversations": []}


def test_etape_affichee_dans_la_langue_de_l_interface(app, user_client):
    """« Pourquoi j'attends » : en attente sans étape = derrière une autre question ; en cours =
    l'étape écrite par le worker, libellée dans la langue de la requête ; rien une fois terminée."""
    from transcria.ask_meetings.models import STAGE_LLM_LOADING

    with app.app_context():
        req = AskStore.create_request(user_id=user_client.user_id, question="q")
        req_id = req.id
    body = user_client.client.get(f"/api/meetings/ask/{req_id}").get_json()
    assert body["stage"] == "queued" and body["stage_label"].startswith("Une autre question")
    with app.app_context():
        AskStore.claim_next()
        AskStore.set_stage(req_id, STAGE_LLM_LOADING)
    body = user_client.client.get(f"/api/meetings/ask/{req_id}?lang=en").get_json()
    assert body["stage"] == "llm_loading" and body["stage_label"] == "Loading the arbitration LLM (one to two minutes the first time)."
    with app.app_context():
        AskStore.complete(req_id, answer="ok", cited_job_ids=[], level_reached=1)
    body = user_client.client.get(f"/api/meetings/ask/{req_id}").get_json()
    assert body["stage"] == "" and body["stage_label"] == ""


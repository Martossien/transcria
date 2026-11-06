"""« Discuter avec mes réunions » — store, filtres de visibilité et worker avec fausse LLM
(docs/DISCUTER_REUNIONS.md § 2.1, 2.2, 2.5, 2.6, 2.7, 2.10). Noms = substituts neutres.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from builders import make_job
from sqlalchemy import text

from transcria.ask_meetings import worker as worker_mod
from transcria.ask_meetings.candidates import visible_candidates
from transcria.ask_meetings.models import (
    LEVEL_CARDS,
    LEVEL_SUMMARIES,
    LEVEL_TRANSCRIPT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_WAITING,
    AskRequest,
    AskTurn,
    AskTurnCitation,
)
from transcria.ask_meetings.store import AskStore, AskValidationError
from transcria.ask_meetings.worker import OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_LLM_BUSY, OUTCOME_SKIPPED, process_next
from transcria.audit.models import AuditLog
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore

CONTEXT = "context/meeting_context.json"


@pytest.fixture
def ctx(app):
    """Contexte d'app + tables de demandes VIDES : la base est partagée par la session de tests,
    et le claim prend la plus ancienne demande en attente, quel qu'en soit l'auteur."""
    with app.app_context():
        db.session.execute(db.delete(AskTurnCitation))
        db.session.execute(db.delete(AskTurn))
        db.session.execute(db.delete(AskRequest))
        db.session.commit()
        yield
        db.session.rollback()


def _user(role=Role.OPERATOR):
    return UserStore.create_user(username=f"ask_{uuid.uuid4().hex[:8]}", password="mdp-de-test", role=role)


def _meeting(owner_id: str, jobs_dir, *, title: str, topic: str = "", decisions=(), actions=(),
             summary: str = "", meeting_type: str = "Réunion interne", srt: str = ""):
    job = make_job(owner_id, title=title)
    fs = JobFilesystem(str(jobs_dir), job.id)
    fs.save_json(CONTEXT, {
        "title": title, "meeting_type": meeting_type, "topic": topic, "date": "2026-05-12",
        "summary": summary or f"Synthèse de {title}.",
        "structured_data": {"decisions": list(decisions), "actions": list(actions)},
    })
    if srt:
        fs.save_text("metadata/transcription_corrigee.srt", srt)
    return job


class FakeRunner:
    """Allocateur + VRAM réduits à leur surface : verrou LLM, réservation, libération."""

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.allocator = self
        self.vram = SimpleNamespace()

    def try_acquire_llm(self, owner: str = "", timeout_s: float = 0) -> bool:
        self.acquired.append(owner)
        return not self.busy

    def release_llm(self, owner=None) -> None:
        self.released.append(owner)

    def release_phase(self, owner, phase) -> None:
        self.released.append(f"phase:{owner}")


class FakeLLM:
    """Répond selon le niveau détecté dans le contexte envoyé ; mémorise les messages."""

    def __init__(self, answers: dict[int, str]):
        self.answers = answers
        self.calls: list[list[dict]] = []

    def __call__(self, config, messages, *, timeout_s, max_tokens) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        level = 3 if "### Transcription [" in system else (2 if "### Synthèse [" in system else 1)
        return self.answers.get(level, "")

    def sent_meetings(self, call_index: int) -> list[str]:
        """Titres des réunions numérotées envoyées à l'appel ``call_index``."""
        import re
        return re.findall(r"^\[\d+\] (.+)$", self.calls[call_index][0]["content"], flags=re.MULTILINE)


@pytest.fixture
def quiet_llm_prep(monkeypatch):
    monkeypatch.setattr(worker_mod, "prepare_llm", lambda runner, owner, config, *, llm_was_already_running: (None, False))
    monkeypatch.setattr(worker_mod, "llm_running_before_phase", lambda vram, config: True)


def _config(jobs_dir, **ask) -> dict:
    return {"storage": {"jobs_dir": str(jobs_dir), "shared_backend": "fs"},
            "workflow": {"ask_meetings": {"timeout_s": 900, **ask}}}


def _run(config, req, llm, runner=None):
    return worker_mod.AskWorker(config, completion=llm, runner=runner or FakeRunner()).process(req)


def _claimed(req_id: str) -> AskRequest:
    db.session.expire_all()
    req = AskStore.get(req_id)
    assert req is not None
    return req


# ── Store : validation et cycle de vie ───────────────────────────────────────

@pytest.mark.parametrize(("question", "code"), [
    ("", "empty"), ("   ", "empty"), (None, "empty"), ("x" * 2001, "too_long"),
])
def test_creation_refuse_une_question_vide_ou_trop_longue(ctx, owner_id, question, code):
    with pytest.raises(AskValidationError) as exc:
        AskStore.create_request(user_id=owner_id, question=question)
    assert exc.value.code == code and exc.value.message


def test_creation_accepte_les_limites_et_normalise(ctx, owner_id):
    one = AskStore.create_request(user_id=owner_id, question="é")
    assert one.question == "é" and one.status == STATUS_WAITING and one.level_requested == LEVEL_CARDS
    assert uuid.UUID(one.conversation_id)
    exotic = AskStore.create_request(user_id=owner_id, question="  会議の決定は？ 🎯  ", locale="en-GB",
                                     conversation_id=one.conversation_id.upper())
    assert exotic.question == "会議の決定は？ 🎯" and exotic.locale == "en"          # langue connue, normalisée
    assert AskStore.create_request(user_id=owner_id, question="q", locale="xx-ZZ").locale == "fr"
    assert exotic.conversation_id == one.conversation_id
    longest = AskStore.create_request(user_id=owner_id, question="q" * 2000)
    assert len(longest.question) == 2000


def test_creation_valide_niveau_cible_et_conversation(ctx, owner_id):
    with pytest.raises(AskValidationError, match="Niveau"):
        AskStore.create_request(user_id=owner_id, question="q", level_requested=2)
    with pytest.raises(AskValidationError, match="Niveau"):
        AskStore.create_request(user_id=owner_id, question="q", level_requested="trois")
    with pytest.raises(AskValidationError, match="cible"):
        AskStore.create_request(user_id=owner_id, question="q", level_requested=3)
    with pytest.raises(AskValidationError, match="réunion invalide"):
        AskStore.create_request(user_id=owner_id, question="q", level_requested=3, target_job_id="../etc")
    with pytest.raises(AskValidationError, match="conversation"):
        AskStore.create_request(user_id=owner_id, question="q", conversation_id="pas-un-uuid")


def test_claim_est_atomique_et_fifo(ctx, owner_id):
    first = AskStore.create_request(user_id=owner_id, question="première")
    second = AskStore.create_request(user_id=owner_id, question="seconde")
    claimed = AskStore.claim_next()
    assert claimed is not None and claimed.id == first.id and claimed.status == STATUS_RUNNING
    assert claimed.started_at is not None
    assert AskStore.claim_next().id == second.id
    # Plus rien d'attente : la demande suivante n'existe pas
    AskStore.release(second.id)
    assert _claimed(second.id).status == STATUS_WAITING and _claimed(second.id).started_at is None


def test_claim_concurrent_ne_prend_jamais_deux_fois(ctx, owner_id):
    req = AskStore.create_request(user_id=owner_id, question="unique")
    # Un autre process a déjà pris la demande entre notre SELECT et notre UPDATE : rowcount 0.
    db.session.execute(text("UPDATE ask_requests SET status = 'running' WHERE id = :id"), {"id": req.id})
    db.session.commit()
    assert AskStore.claim_next() is None


def test_complete_fail_et_lecture_par_proprietaire(ctx, owner_id):
    other = _user()
    req = AskStore.create_request(user_id=owner_id, question="q")
    assert AskStore.get_for_user(req.id, owner_id) is not None
    assert AskStore.get_for_user(req.id, other.id) is None
    AskStore.complete(req.id, answer="trop tôt", cited_job_ids=[], level_reached=1)
    assert _claimed(req.id).status == STATUS_WAITING                  # pas en cours : rien n'est écrit
    AskStore.claim_next()
    AskStore.complete(req.id, answer="Réponse [1]", cited_job_ids=["j1", "j1", "j2"], level_reached=2)
    done = _claimed(req.id)
    assert (done.status, done.answer, done.cited_job_ids, done.level_reached) == (STATUS_DONE, "Réponse [1]", ["j1", "j1", "j2"], 2)
    assert done.done_at is not None
    AskStore.fail(req.id, "trop tard : déjà terminée")
    assert _claimed(req.id).status == STATUS_DONE
    late = AskStore.create_request(user_id=owner_id, question="q2")
    AskStore.claim_next()
    AskStore.fail(late.id, "x" * 5000)
    assert _claimed(late.id).status == STATUS_FAILED and len(_claimed(late.id).error) == 2000


def test_fail_stale_expire_les_demandes_oubliees(ctx, owner_id):
    stuck = AskStore.create_request(user_id=owner_id, question="bloquée")
    old = AskStore.create_request(user_id=owner_id, question="vieille")
    fresh = AskStore.create_request(user_id=owner_id, question="fraîche")
    ago = datetime.now(UTC) - timedelta(hours=2)
    db.session.execute(text("UPDATE ask_requests SET status='running', started_at=:t WHERE id=:id"), {"t": ago, "id": stuck.id})
    db.session.execute(text("UPDATE ask_requests SET created_at=:t WHERE id=:id"), {"t": ago, "id": old.id})
    db.session.commit()
    assert AskStore.fail_stale(900) == 2
    assert _claimed(stuck.id).status == STATUS_FAILED and _claimed(old.id).status == STATUS_FAILED
    assert _claimed(fresh.id).status == STATUS_WAITING


def test_tours_isoles_par_utilisateur_et_conversation(ctx, owner_id):
    other = _user()
    conv = str(uuid.uuid4())
    for i in range(5):
        AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text=f"q{i}")
        AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text=f"r{i}", level=1)
    AskStore.append_turn(conversation_id=conv, user_id=other.id, role="user", text="intrus")
    recent = AskStore.recent_turns(conv, owner_id, 4)
    assert [t.text for t in recent] == ["q3", "r3", "q4", "r4"]
    assert AskStore.recent_turns(conv, other.id, 10)[0].text == "intrus" and len(AskStore.recent_turns(conv, other.id, 10)) == 1
    assert AskStore.recent_turns(conv, owner_id, 0) == []
    assert AskStore.recent_turns(str(uuid.uuid4()), owner_id, 10) == []


def test_purge_de_retention(ctx, owner_id):
    conv = str(uuid.uuid4())
    old = AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text="vieux", cited_job_ids=[])
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text="récent")
    old_req = AskStore.create_request(user_id=owner_id, question="vieille")
    AskStore.create_request(user_id=owner_id, question="récente")
    ago = datetime.now(UTC) - timedelta(days=40)
    db.session.execute(text("UPDATE ask_turns SET created_at=:t WHERE id=:id"), {"t": ago, "id": old.id})
    db.session.execute(text("UPDATE ask_requests SET created_at=:t WHERE id=:id"), {"t": ago, "id": old_req.id})
    db.session.commit()
    old_req_id = old_req.id
    assert AskStore.purge_expired(0) == 0 and AskStore.purge_expired(None) == 0
    assert AskStore.purge_expired(30) == 2
    db.session.expire_all()
    assert [t.text for t in AskStore.recent_turns(conv, owner_id, 10)] == ["récent"]
    assert AskStore.get(old_req_id) is None


def test_supprimer_une_reunion_supprime_les_tours_qui_la_citent(ctx, owner_id, tmp_path):
    cited = _meeting(owner_id, tmp_path, title="Citée")
    kept_job = _meeting(owner_id, tmp_path, title="Autre")
    conv = str(uuid.uuid4())
    citing = AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text="… [1]",
                                  cited_job_ids=[cited.id, kept_job.id])
    unrelated = AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text="… [1]",
                                     cited_job_ids=[kept_job.id])
    plain = AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text="question")
    cited_id, citing_id = cited.id, citing.id
    assert JobStore.delete_job(cited_id) is True
    db.session.expire_all()
    assert db.session.get(AskTurn, citing_id) is None
    assert db.session.get(AskTurn, unrelated.id) is not None and db.session.get(AskTurn, plain.id) is not None
    assert db.session.execute(text("SELECT count(*) FROM ask_turn_citations WHERE turn_id=:t"), {"t": citing_id}).scalar() == 0


# ── Filtres AVANT la LLM ─────────────────────────────────────────────────────

def test_admin_voit_tout_membre_ne_voit_que_ses_groupes(ctx, tmp_path):
    admin = _user(Role.ADMIN)
    member = _user()
    colleague = _user()
    stranger = _user()
    group = GroupStore.create_group(f"g-{uuid.uuid4().hex[:6]}")
    GroupStore.add_member(group.id, member.id)
    GroupStore.add_member(group.id, colleague.id)
    mine = _meeting(member.id, tmp_path, title="La mienne")
    theirs = _meeting(colleague.id, tmp_path, title="Du collègue")
    secret = _meeting(stranger.id, tmp_path, title="D'un inconnu")
    cfg = _config(tmp_path)

    seen_by_member = {c.job.id for c in visible_candidates(cfg, member, include_confidential=False).candidates}
    assert seen_by_member == {mine.id, theirs.id}
    seen_by_admin = {c.job.id for c in visible_candidates(cfg, admin, include_confidential=False).candidates}
    assert {mine.id, theirs.id, secret.id} <= seen_by_admin


def test_confidentielles_exclues_sauf_demande_explicite(ctx, owner_id, tmp_path):
    user = UserStore.get_by_id(owner_id)
    _meeting(owner_id, tmp_path, title="Normale")
    job = make_job(owner_id, title="Confidentielle")
    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"meeting_type": "Réunion interne", "sensitivity": "high"})
    without_card = make_job(owner_id, title="Sans contexte")
    cfg = _config(tmp_path)
    default = visible_candidates(cfg, user, include_confidential=False)
    assert {c.job.title for c in default.candidates} == {"Normale"}
    assert default.excluded_confidential == 1 and default.without_card == 1
    included = visible_candidates(cfg, user, include_confidential=True)
    assert {c.job.title for c in included.candidates} == {"Normale", "Confidentielle"}
    assert without_card.id not in {c.job.id for c in included.candidates}


def test_en_cloud_les_reunions_des_groupes_interdits_et_des_connecteurs_sont_exclues(ctx, tmp_path, monkeypatch):
    from transcria.ask_meetings import candidates as cand_mod

    admin = _user(Role.ADMIN)
    forbidden_owner = _user()
    plain_owner = _user()
    group = GroupStore.create_group(f"interdit-{uuid.uuid4().hex[:6]}")
    GroupStore.add_member(group.id, forbidden_owner.id)
    db.session.execute(text("UPDATE groups SET llm_cloud_forbidden = true WHERE id = :id"), {"id": group.id})
    db.session.commit()
    forbidden = _meeting(forbidden_owner.id, tmp_path, title="Interdite")
    allowed = _meeting(plain_owner.id, tmp_path, title="Autorisée")
    connector = _meeting(plain_owner.id, tmp_path, title="Captée")
    JobStore.update_extra_data(connector.id, lambda e: {**e, "source": "meeting"})
    cfg = _config(tmp_path)

    local = {c.job.id for c in visible_candidates(cfg, admin, include_confidential=False).candidates}
    assert {forbidden.id, allowed.id, connector.id} <= local            # en local, rien d'exclu

    monkeypatch.setattr(cand_mod, "is_cloud_backend", lambda config: True)
    monkeypatch.setattr(cand_mod.LlmCloudView, "from_config",
                        classmethod(lambda cls, config: SimpleNamespace(provider="p", model="m")))
    monkeypatch.setattr(cand_mod.LlmCloudStore, "current",
                        staticmethod(lambda provider, model: SimpleNamespace(connectors_allowed=False)))
    cloud = visible_candidates(cfg, admin, include_confidential=False)
    ids = {c.job.id for c in cloud.candidates}
    assert allowed.id in ids and forbidden.id not in ids and connector.id not in ids
    assert cloud.excluded_cloud >= 2

    monkeypatch.setattr(cand_mod.LlmCloudStore, "current",
                        staticmethod(lambda provider, model: SimpleNamespace(connectors_allowed=True)))
    ids = {c.job.id for c in visible_candidates(cfg, admin, include_confidential=False).candidates}
    assert connector.id in ids and forbidden.id not in ids


# ── Worker : escalade, garde, écriture ───────────────────────────────────────

def test_niveau_1_repond_avec_citations_verifiees(ctx, owner_id, tmp_path, quiet_llm_prep):
    budget = _meeting(owner_id, tmp_path, title="Budget formation", decisions=["Budget validé"])
    _meeting(owner_id, tmp_path, title="Point projet")
    req = AskStore.create_request(user_id=owner_id, question="qu'a-t-on décidé sur le budget ?")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Le budget a été validé [1]. Hors liste [9]."})
    runner = FakeRunner()

    assert _run(_config(tmp_path), req, llm, runner) == OUTCOME_DONE
    done = _claimed(req.id)
    assert done.status == STATUS_DONE and done.level_reached == LEVEL_CARDS
    assert done.answer == "Le budget a été validé [1]. Hors liste."
    assert done.cited_job_ids == [budget.id]
    assert llm.sent_meetings(0)[0] == "Budget formation"            # la plus pertinente en [1]
    assert len(llm.calls) == 1
    turns = AskStore.recent_turns(req.conversation_id, owner_id, 10)
    assert [t.role for t in turns] == ["user", "assistant"] and turns[1].cited_job_ids == [budget.id]
    assert runner.acquired == [f"ask:{req.id}"] and runner.released[-1] == f"ask:{req.id}"
    audit = db.session.execute(db.select(AuditLog).where(AuditLog.target_id == req.conversation_id)).scalars().all()
    assert len(audit) == 1 and audit[0].action == "job_ask_meetings"
    assert "budget" not in (audit[0].details_json or "").lower()        # jamais la question ni la réponse


def test_sans_citation_le_niveau_2_lit_les_syntheses(ctx, owner_id, tmp_path, quiet_llm_prep):
    near = _meeting(owner_id, tmp_path, title="Prestataire nettoyage", topic="choix du prestataire",
                    summary="Le prestataire retenu est Organisation A, contrat de deux ans.")
    _meeting(owner_id, tmp_path, title="Autre réunion")
    req = AskStore.create_request(user_id=owner_id, question="quel prestataire a été retenu ?")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Non retrouvé dans les réunions consultées.", 2: "Organisation A, pour deux ans [1]."})

    assert _run(_config(tmp_path, level2_meetings=1), req, llm) == OUTCOME_DONE
    done = _claimed(req.id)
    assert done.level_reached == LEVEL_SUMMARIES and done.cited_job_ids == [near.id]
    assert len(llm.calls) == 2
    assert "contrat de deux ans" in llm.calls[1][0]["content"]        # la synthèse effective a été envoyée
    assert "### Synthèse [1]" in llm.calls[1][0]["content"]
    assert llm.sent_meetings(1) == ["Prestataire nettoyage"]


def test_rien_retrouve_aux_deux_niveaux_le_dit(ctx, owner_id, tmp_path, quiet_llm_prep):
    _meeting(owner_id, tmp_path, title="Réunion A")
    req = AskStore.create_request(user_id=owner_id, question="la couleur du ciel ?", locale="en")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Not found.", 2: "Still not found, maybe [4]."})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_DONE
    done = _claimed(req.id)
    assert done.level_reached == LEVEL_SUMMARIES and done.cited_job_ids == []
    assert done.answer == "None of the meetings you can access answers this question."


def test_niveau_3_lit_la_transcription_de_la_cible_seule(ctx, owner_id, tmp_path, quiet_llm_prep):
    target = _meeting(owner_id, tmp_path, title="Cible", srt="1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00: la phrase exacte\n")
    _meeting(owner_id, tmp_path, title="Voisine")
    req = AskStore.create_request(user_id=owner_id, question="qu'a dit exactement SPEAKER_00 ?",
                                  level_requested=3, target_job_id=target.id)
    req = AskStore.claim_next()
    llm = FakeLLM({3: "Il a dit « la phrase exacte »."})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_DONE
    done = _claimed(req.id)
    assert done.level_reached == LEVEL_TRANSCRIPT and done.cited_job_ids == [target.id]
    assert len(llm.calls) == 1 and llm.sent_meetings(0) == ["Cible"]
    assert "la phrase exacte" in llm.calls[0][0]["content"]
    actions = {a.action for a in db.session.execute(
        db.select(AuditLog).where(AuditLog.actor_id == owner_id)).scalars().all()}
    assert {"job_ask_meetings", "job_ask_meetings_transcript"} <= actions


def test_niveau_3_sans_transcription_le_dit(ctx, owner_id, tmp_path, quiet_llm_prep):
    target = _meeting(owner_id, tmp_path, title="Sans SRT")
    req = AskStore.create_request(user_id=owner_id, question="q", level_requested=3, target_job_id=target.id)
    req = AskStore.claim_next()
    llm = FakeLLM({})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_DONE
    assert _claimed(req.id).answer == "La transcription de cette réunion n'est pas disponible."
    assert llm.calls == []


def test_niveau_3_sur_une_reunion_non_visible_est_refuse(ctx, owner_id, tmp_path, quiet_llm_prep):
    stranger = _user()
    hidden = _meeting(stranger.id, tmp_path, title="Pas à moi", srt="1\n00:00:00,000 --> 00:00:01,000\nx\n")
    _meeting(owner_id, tmp_path, title="À moi")
    req = AskStore.create_request(user_id=owner_id, question="q", level_requested=3, target_job_id=hidden.id)
    req = AskStore.claim_next()
    llm = FakeLLM({3: "Fuite [1]"})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_FAILED
    done = _claimed(req.id)
    assert done.status == STATUS_FAILED and "ne fait pas partie" in done.error
    assert llm.calls == []


def test_llm_occupee_remet_la_demande_en_attente(ctx, owner_id, tmp_path, quiet_llm_prep):
    _meeting(owner_id, tmp_path, title="A")
    req = AskStore.create_request(user_id=owner_id, question="q")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "x [1]"})
    assert _run(_config(tmp_path), req, llm, FakeRunner(busy=True)) == OUTCOME_LLM_BUSY
    assert _claimed(req.id).status == STATUS_WAITING and llm.calls == []


def test_desactive_sans_reunion_ou_expiree(ctx, owner_id, tmp_path, quiet_llm_prep):
    req = AskStore.create_request(user_id=owner_id, question="q", locale="de")
    req = AskStore.claim_next()
    assert _run(_config(tmp_path, enabled=False), req, FakeLLM({})) == OUTCOME_FAILED
    assert "Administrator" in _claimed(req.id).error

    req2 = AskStore.create_request(user_id=owner_id, question="q")
    req2 = AskStore.claim_next()
    assert _run(_config(tmp_path), req2, FakeLLM({})) == OUTCOME_FAILED
    assert "Aucune réunion" in _claimed(req2.id).error

    _meeting(owner_id, tmp_path, title="A")
    req3 = AskStore.create_request(user_id=owner_id, question="q")
    db.session.execute(text("UPDATE ask_requests SET created_at=:t WHERE id=:id"),
                       {"t": datetime.now(UTC) - timedelta(hours=1), "id": req3.id})
    db.session.commit()
    req3 = AskStore.claim_next()
    assert _run(_config(tmp_path), req3, FakeLLM({1: "x [1]"})) == OUTCOME_FAILED
    assert "expiré" in _claimed(req3.id).error


def test_exception_de_la_llm_devient_un_echec_lisible(ctx, owner_id, tmp_path, quiet_llm_prep):
    _meeting(owner_id, tmp_path, title="A")
    req = AskStore.create_request(user_id=owner_id, question="q")
    req = AskStore.claim_next()

    def boom(config, messages, *, timeout_s, max_tokens):
        raise RuntimeError("fournisseur injoignable")

    runner = FakeRunner()
    assert _run(_config(tmp_path), req, boom, runner) == OUTCOME_FAILED
    done = _claimed(req.id)
    assert done.status == STATUS_FAILED and "erreur interne" in done.error
    assert "fournisseur injoignable" not in done.error                # le détail reste dans le journal
    assert runner.released[-1] == f"ask:{req.id}"                    # verrou rendu malgré l'erreur


def test_l_historique_est_rejoue_et_les_plafonds_respectes(ctx, owner_id, tmp_path, quiet_llm_prep):
    for i in range(6):
        _meeting(owner_id, tmp_path, title=f"Réunion {i} budget")
    conv = str(uuid.uuid4())
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text="première question")
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text="première réponse [1]", level=1)
    req = AskStore.create_request(user_id=owner_id, question="et le budget ?", conversation_id=conv)
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Suite [2]."})
    assert _run(_config(tmp_path, max_cards=3, context_turns=2), req, llm) == OUTCOME_DONE
    messages = llm.calls[0]
    # Les [n] d'un tour précédent numérotaient une AUTRE liste : retirés du rejeu.
    assert [m["content"] for m in messages[1:]] == ["première question", "première réponse", "et le budget ?"]
    assert len(llm.sent_meetings(0)) == 3


def test_process_next_prend_traite_et_rend_skipped_quand_rien(ctx, owner_id, tmp_path, quiet_llm_prep):
    assert process_next(_config(tmp_path), completion=FakeLLM({}), runner=FakeRunner()) == OUTCOME_SKIPPED
    _meeting(owner_id, tmp_path, title="A")
    req = AskStore.create_request(user_id=owner_id, question="q")
    assert process_next(_config(tmp_path), completion=FakeLLM({1: "ok [1]"}), runner=FakeRunner()) == OUTCOME_DONE
    assert _claimed(req.id).status == STATUS_DONE


# ── Relecture 2026-11-06 : ce que la LLM ne doit JAMAIS recevoir, et les reprises ─────────

def test_l_historique_ne_rejoue_pas_une_reponse_citant_une_reunion_hors_candidats(ctx, owner_id, tmp_path, quiet_llm_prep):
    """Réponse donnée AVEC les confidentielles, question suivante SANS la case : le texte qui
    citait la réunion confidentielle ne repart pas vers la LLM (même mécanique pour une réunion
    passée sous interdiction cloud ou captée par connecteur)."""
    normal = _meeting(owner_id, tmp_path, title="Normale budget")
    secret = make_job(owner_id, title="Confidentielle")
    JobFilesystem(str(tmp_path), secret.id).save_json(CONTEXT, {"meeting_type": "Réunion interne", "sensitivity": "high",
                                                                "title": "Confidentielle budget"})
    conv = str(uuid.uuid4())
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="user", text="question avec confidentielles")
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text="RÉPONSE SECRÈTE [2]", level=1,
                         cited_job_ids=[secret.id, normal.id])
    AskStore.append_turn(conversation_id=conv, user_id=owner_id, role="assistant", text="réponse publique [1]", level=1,
                         cited_job_ids=[normal.id])
    req = AskStore.create_request(user_id=owner_id, question="et le budget ?", conversation_id=conv)
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Suite [1]."})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_DONE
    sent = "\n".join(m["content"] for m in llm.calls[0])
    assert "RÉPONSE SECRÈTE" not in sent and "Confidentielle budget" not in sent
    assert "question avec confidentielles" in sent and "réponse publique" in sent


def test_une_reunion_invisible_n_atteint_jamais_la_llm(ctx, owner_id, tmp_path, quiet_llm_prep):
    stranger = _user()
    _meeting(stranger.id, tmp_path, title="Réunion secrète budget", decisions=["Décision secrète"])
    _meeting(owner_id, tmp_path, title="Ma réunion budget")
    req = AskStore.create_request(user_id=owner_id, question="budget")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "x [1][2]"})
    assert _run(_config(tmp_path), req, llm) == OUTCOME_DONE
    sent = "\n".join(m["content"] for m in llm.calls[0])
    assert "secrète" not in sent.lower() and llm.sent_meetings(0) == ["Ma réunion budget"]
    assert _claimed(req.id).answer == "x [1]"                        # [2] n'existait pas : retiré


def test_aucune_fiche_dans_le_budget_rien_ne_part_a_la_llm(ctx, owner_id, tmp_path, quiet_llm_prep):
    _meeting(owner_id, tmp_path, title="Grosse réunion", decisions=[f"Décision {i} : " + "détail " * 12 for i in range(40)])
    req = AskStore.create_request(user_id=owner_id, question="grosse")
    req = AskStore.claim_next()
    llm = FakeLLM({1: "Texte libre non vérifiable"})
    assert _run(_config(tmp_path, max_context_tokens=500), req, llm) == OUTCOME_DONE
    assert llm.calls == []
    assert _claimed(req.id).answer.startswith("Aucune des réunions")


def test_vram_insuffisante_remet_en_attente_reservation_liberee(ctx, owner_id, tmp_path, monkeypatch):
    _meeting(owner_id, tmp_path, title="A")
    monkeypatch.setattr(worker_mod, "llm_running_before_phase", lambda vram, config: False)
    monkeypatch.setattr(worker_mod, "prepare_llm",
                        lambda runner, owner, config, *, llm_was_already_running: ("vram_insufficient", False))
    req = AskStore.create_request(user_id=owner_id, question="q")
    req = AskStore.claim_next()
    runner = FakeRunner()
    assert _run(_config(tmp_path), req, FakeLLM({1: "x [1]"}), runner) == OUTCOME_LLM_BUSY
    assert _claimed(req.id).status == STATUS_WAITING

    monkeypatch.setattr(worker_mod, "prepare_llm",
                        lambda runner, owner, config, *, llm_was_already_running: (None, True))
    req = AskStore.claim_next()
    runner = FakeRunner()
    assert _run(_config(tmp_path), req, FakeLLM({1: "x [1]"}), runner) == OUTCOME_DONE
    assert runner.released == [f"phase:ask:{req.id}", f"ask:{req.id}"]   # réservation puis verrou

    monkeypatch.setattr(worker_mod, "prepare_llm",
                        lambda runner, owner, config, *, llm_was_already_running: ("llm_unavailable", False))
    req2 = AskStore.create_request(user_id=owner_id, question="q")
    req2 = AskStore.claim_next()
    assert _run(_config(tmp_path), req2, FakeLLM({}), FakeRunner()) == OUTCOME_FAILED
    assert "n'a pas pu être démarrée" in _claimed(req2.id).error


def test_reunion_supprimee_pendant_l_appel_llm(ctx, owner_id, tmp_path, quiet_llm_prep):
    """La réunion citée disparaît pendant la génération : pas d'erreur d'intégrité, la demande se
    termine sans la citation fantôme, et la session reste saine."""
    doomed = _meeting(owner_id, tmp_path, title="Éphémère budget")
    kept = _meeting(owner_id, tmp_path, title="Durable budget")
    req = AskStore.create_request(user_id=owner_id, question="budget")
    req = AskStore.claim_next()
    doomed_id = doomed.id

    def deleting_llm(config, messages, *, timeout_s, max_tokens):
        JobStore.delete_job(doomed_id)
        return "Les deux [1] et [2]."

    assert _run(_config(tmp_path), req, deleting_llm) == OUTCOME_DONE
    done = _claimed(req.id)
    assert done.status == STATUS_DONE and done.cited_job_ids == [kept.id]
    turns = AskStore.recent_turns(req.conversation_id, owner_id, 10)
    assert [t.role for t in turns] == ["user", "assistant"] and turns[1].cited_job_ids == [kept.id]


def test_echec_en_fin_de_traitement_laisse_une_demande_en_echec_lisible(ctx, owner_id, tmp_path, quiet_llm_prep, monkeypatch):
    _meeting(owner_id, tmp_path, title="A")
    req = AskStore.create_request(user_id=owner_id, question="q")
    req = AskStore.claim_next()

    def broken_complete(*a, **kw):
        db.session.execute(text("SELECT 1 / 0"))        # transaction avortée côté PostgreSQL

    monkeypatch.setattr(worker_mod.AskStore, "complete", staticmethod(broken_complete))
    assert _run(_config(tmp_path), req, FakeLLM({1: "x [1]"})) == OUTCOME_FAILED
    done = _claimed(req.id)
    assert done.status == STATUS_FAILED and "erreur interne" in done.error
    assert db.session.execute(text("SELECT 1")).scalar() == 1


def test_supprimer_une_reunion_efface_aussi_les_demandes_qui_la_citent(ctx, owner_id, tmp_path):
    cited = _meeting(owner_id, tmp_path, title="Citée")
    req = AskStore.create_request(user_id=owner_id, question="q")
    AskStore.claim_next()
    AskStore.complete(req.id, answer="réponse", cited_job_ids=[cited.id], level_reached=1)
    other = AskStore.create_request(user_id=owner_id, question="q2")
    req_id, other_id, cited_id = req.id, other.id, cited.id
    JobStore.delete_job(cited_id)
    db.session.expire_all()
    assert AskStore.get(req_id) is None and AskStore.get(other_id) is not None


def test_cible_inexistante_acceptee_puis_dite_invisible(ctx, owner_id, tmp_path, quiet_llm_prep):
    """Pas d'oracle d'existence : un identifiant inconnu reçoit le même sort qu'une réunion invisible."""
    _meeting(owner_id, tmp_path, title="A")
    req = AskStore.create_request(user_id=owner_id, question="q", level_requested=3, target_job_id=str(uuid.uuid4()))
    req = AskStore.claim_next()
    assert _run(_config(tmp_path), req, FakeLLM({3: "x"})) == OUTCOME_FAILED
    assert "ne fait pas partie" in _claimed(req.id).error


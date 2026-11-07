"""Fiches de réunion en base : la fiche est le REFLET du contexte, sur tous les chemins d'écriture
(docs/DISCUTER_REUNIONS.md § 2.3, lot A). Noms = substituts neutres.
"""
from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from builders import make_job
from flask import Flask
from sqlalchemy import text

from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.context.meeting_context import MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.database import db, import_all_models
from transcria.job_cards import hooks
from transcria.job_cards import store as store_mod
from transcria.job_cards.models import JobCard
from transcria.job_cards.store import CREATED, SKIPPED, UNCHANGED, UPDATED, JobCardStore
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.maintenance import cli
from transcria.workflow.refine_store import RefineStore
from transcria.workflow.speaker_projection import merge_llm_suggestions

CONTEXT = "context/meeting_context.json"
PARTICIPANTS = "context/participants.json"


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield
        db.session.rollback()


@pytest.fixture
def job(ctx, owner_id):
    return make_job(owner_id, title="Réunion fiche")


def _card(job_id: str) -> JobCard | None:
    db.session.expire_all()
    return JobCardStore.get(job_id)


# ── La fiche suit chaque écriture ────────────────────────────────────────────

def test_ecriture_directe_du_contexte_cree_la_fiche(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {
        "title_suggere": "Point d'avancement", "type_suggere": "Réunion projet", "date": "2026-02-10",
        "language": "fr", "sujet_suggere": "Avancement du projet", "objectif_suggere": "Valider le budget",
        "notes_suggeres": "1. Budget\n2. Planning",
        "summary_llm": "## Synthèse\nLe budget est validé.\n\n## Participants probables\n- SPEAKER_00",
        "structured_data": {"decisions": ["Budget validé"], "actions": ["Personne A : envoyer le devis (vendredi)"],
                            "prochaine_date": "10 mars"},
        "termes_suspects": [{"term": "ne doit jamais entrer dans la fiche"}],
    })
    card = _card(job.id)
    assert card is not None
    assert card.to_dict() | {"updated_at": None} == {
        "job_id": job.id, "title": "Point d'avancement", "meeting_type": "Réunion projet",
        "meeting_date": "2026-02-10", "date_is_import_fallback": False, "confidential": False,
        "sensitivity": "normal", "language": "fr", "participants": [], "speaking": [], "unnamed_speech_pct": 0,
        "topic": "Avancement du projet", "objective": "Valider le budget", "agenda": "1. Budget 2. Planning",
        "abstract": "Le budget est validé.", "decisions": ["Budget validé"],
        "actions": ["Personne A : envoyer le devis (vendredi)"], "next_date": "10 mars", "updated_at": None,
    }
    assert "ne doit jamais entrer" not in json.dumps(card.to_dict(), ensure_ascii=False)


def test_etape_4_enregistree_met_la_fiche_a_jour(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title_suggere": "Titre machine", "type_suggere": "Réunion projet"})
    assert _card(job.id).confidential is False

    MeetingContextManager.save(job, str(tmp_path), {
        "title": "Titre humain", "meeting_type": "Réunion interne", "sensitivity": "high", "date": "12/05/2026"})
    card = _card(job.id)
    assert (card.title, card.meeting_type, card.confidential, card.sensitivity) == (
        "Titre humain", "Réunion interne", True, "high")
    assert (card.meeting_date, card.date_is_import_fallback) == (date(2026, 5, 12), False)


def test_participants_enregistres_mettent_la_fiche_a_jour(job, tmp_path):
    ParticipantsManager.save(job, str(tmp_path), [
        {"name": "Personne A", "function": "Fonction A"}, {"name": "SPEAKER_02"}])
    card = _card(job.id)
    assert card.participants == [{"name": "Personne A", "function": "Fonction A"}]
    assert card.date_is_import_fallback is True and card.meeting_date is not None


def test_reecriture_identique_ne_touche_pas_la_fiche(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Stable"})
    before = _card(job.id).updated_at
    fs.save_json(CONTEXT, {"title": "Stable"})
    assert _card(job.id).updated_at == before
    assert JobCardStore.refresh(job.id, str(tmp_path)) == UNCHANGED
    fs.save_json(CONTEXT, {"title": "Changé"})
    assert _card(job.id).title == "Changé"


def test_restauration_de_version_remet_la_fiche(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Avant affinage"})
    refine = RefineStore(str(tmp_path), job.id)
    version = refine.snapshot_artifacts([fs.job_dir / CONTEXT, fs.job_dir / PARTICIPANTS])
    fs.save_json(CONTEXT, {"title": "Après affinage"})
    fs.save_json(PARTICIPANTS, [{"name": "Personne B"}])
    assert (_card(job.id).title, len(_card(job.id).participants)) == ("Après affinage", 1)

    refine.restore_version(version)        # copie SANS save_json, et supprime le fichier absent au snapshot
    card = _card(job.id)
    assert (card.title, card.participants) == ("Avant affinage", [])


# ── Robustesse ───────────────────────────────────────────────────────────────

def test_job_inconnu_ou_sans_fichier_local_est_saute(job, tmp_path):
    assert JobCardStore.refresh(str(uuid.uuid4()), str(tmp_path)) == SKIPPED
    assert JobCardStore.refresh(job.id, str(tmp_path)) == SKIPPED
    assert not (tmp_path / job.id).exists()          # la lecture ne crée aucun dossier
    assert _card(job.id) is None


def test_fichier_absent_ici_n_efface_pas_une_fiche_existante(job, tmp_path):
    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"title": "Écrit par un autre tier"})
    other_disk = tmp_path / "autre-disque"
    other_disk.mkdir()
    assert JobCardStore.refresh(job.id, str(other_disk)) == SKIPPED
    assert _card(job.id).title == "Écrit par un autre tier"


def test_contexte_illisible_garde_la_fiche(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Bonne fiche"})
    (fs.job_dir / CONTEXT).write_text("{ tronqué", encoding="utf-8")
    assert JobCardStore.refresh(job.id, str(tmp_path)) == SKIPPED
    assert _card(job.id).title == "Bonne fiche"


def test_formes_inattendues_dans_les_fichiers(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, ["une", "liste"])
    fs.save_json(PARTICIPANTS, {"pas": "une liste"})
    card = _card(job.id)
    assert (card.title, card.participants, card.abstract) == ("", [], "")


def test_echec_du_recalcul_ne_casse_jamais_l_ecriture(job, tmp_path, monkeypatch):
    def boom(job_id, jobs_dir):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(JobCardStore, "refresh", staticmethod(boom))
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Écrit quand même"})
    assert fs.load_json(CONTEXT) == {"title": "Écrit quand même"}
    assert JobStore.get_by_id(job.id) is not None        # session toujours utilisable


def test_hors_contexte_d_application_rien_ne_se_passe(tmp_path):
    fs = JobFilesystem(str(tmp_path), "job-sans-app")
    fs.save_json(CONTEXT, {"title": "Outil hors serveur"})
    assert fs.load_json(CONTEXT) == {"title": "Outil hors serveur"}


def test_app_sans_fiches_n_est_pas_touchee(tmp_path, monkeypatch):
    bare = Flask("app-nue")
    called = []
    monkeypatch.setattr(JobCardStore, "refresh", staticmethod(lambda *a: called.append(a)))
    with bare.app_context():
        JobFilesystem(str(tmp_path), "job-app-nue").save_json(CONTEXT, {"title": "x"})
    assert called == []


def test_erreur_de_base_dans_le_recalcul_laisse_la_session_utilisable(job, tmp_path, monkeypatch):
    """Vraie erreur SQL (transaction avortée sur PostgreSQL) : sans le rollback de l'accroche,
    la requête suivante de l'appelant échouerait."""
    def failing(job_id, jobs_dir):
        db.session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(JobCardStore, "refresh", staticmethod(failing))
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Écrit quand même"})
    assert fs.load_json(CONTEXT) == {"title": "Écrit quand même"}
    assert db.session.execute(text("SELECT 1")).scalar() == 1


# ── Concurrence ──────────────────────────────────────────────────────────────

def test_fichier_modifie_pendant_le_recalcul_la_fiche_finit_juste(job, tmp_path, monkeypatch):
    """Un autre process réécrit le contexte PENDANT que celui-ci calcule sur l'ancien contenu :
    la relecture après écriture rattrape, la fiche ne reste pas sur la version périmée."""
    fs = JobFilesystem(str(tmp_path), job.id)
    real = store_mod.project_card
    calls = []

    def racing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:        # écriture concurrente, sans passer par l'accroche de CE process
            (fs.job_dir / CONTEXT).write_text('{"title": "Version récente"}', encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(store_mod, "project_card", racing)
    fs.save_json(CONTEXT, {"title": "Version ancienne"})
    assert _card(job.id).title == "Version récente"
    assert len(calls) == 2


def test_recalculs_enchaines_bornes(job, tmp_path, monkeypatch):
    """Un fichier qui change à CHAQUE passe ne fait pas boucler le recalcul."""
    fs = JobFilesystem(str(tmp_path), job.id)
    real = store_mod.project_card
    calls = []

    def always_racing(*args, **kwargs):
        calls.append(1)
        (fs.job_dir / CONTEXT).write_text(json.dumps({"title": f"v{len(calls)}"}), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(store_mod, "project_card", always_racing)
    fs.save_json(CONTEXT, {"title": "v0"})
    assert len(calls) == store_mod._MAX_PASSES


def test_deux_creations_simultanees_la_seconde_met_a_jour(job, tmp_path, monkeypatch):
    """Web et worker créent la fiche du même job en même temps : la clé primaire refuse le second
    INSERT, qui devient une mise à jour — aucune exception, fiche conforme au fichier."""
    fs = JobFilesystem(str(tmp_path), job.id)
    real = store_mod.project_card
    job_id = job.id
    inserted = []

    def competitor(*args, **kwargs):
        if not inserted:
            inserted.append(1)
            with db.engine.begin() as other:      # un AUTRE process, sa propre connexion
                other.execute(text(
                    "INSERT INTO job_cards (job_id, source_sha256, updated_at) VALUES (:id, 'autre', now())"),
                    {"id": job_id})
        return real(*args, **kwargs)

    monkeypatch.setattr(store_mod, "project_card", competitor)
    fs.save_json(CONTEXT, {"title": "Écrit par les deux"})
    card = _card(job_id)
    assert card.title == "Écrit par les deux" and card.source_sha256 != "autre"


# ── Stockage partagé pg : le disque local n'est qu'un cache ──────────────────

def test_rattrapage_en_stockage_pg_tire_le_contexte_avant_de_recalculer(app, job, tmp_path, monkeypatch):
    """Scénario de la relecture : le worker garde un contexte d'AVANT l'étape 4 ; l'utilisateur a
    choisi depuis un type confidentiel sur le tier web (fiche confidentielle, fichier en base).
    Au redémarrage du worker, le rattrapage ne doit pas recalculer sur le cache périmé."""
    confidential_type = sorted(store_mod.confidential_types())[0]
    fresh = {"meeting_type": confidential_type, "title": "Validée sur le web"}
    stale = {"type_suggere": "Réunion interne", "title_suggere": "Avant validation"}
    job_dir = tmp_path / job.id / "context"

    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, fresh)        # état juste, fiche juste
    assert _card(job.id).confidential is True
    (job_dir / "meeting_context.json").write_text(json.dumps(stale), encoding="utf-8")   # cache périmé

    pulled = []

    def fake_pull(cfg, job_id, *, prefixes):
        pulled.append((job_id, prefixes))
        if job_id == job.id:
            (job_dir / "meeting_context.json").write_text(json.dumps(fresh), encoding="utf-8")

    monkeypatch.setattr(store_mod, "is_pg_backend", lambda cfg: True)
    monkeypatch.setattr(store_mod, "pull_job_files", fake_pull)
    thread = hooks.start_card_catchup(app, {"storage": {"jobs_dir": str(tmp_path), "shared_backend": "pg"}})
    thread.join(timeout=30)

    assert (job.id, ("context/",)) in pulled
    card = _card(job.id)
    assert (card.confidential, card.title) == (True, "Validée sur le web")


# ── « Étape 4 enregistrée » : seul un humain pose meeting_type ───────────────

def test_les_suggestions_de_la_llm_ne_posent_jamais_meeting_type():
    ctx: dict = {}
    merge_llm_suggestions(ctx, {
        "title_suggere": "Titre", "type_suggere": "Réunion projet", "sujet_suggere": "Sujet",
        "objectif_suggere": "Objectif", "notes_suggeres": "Notes", "participants_detectes": "SPEAKER_00",
        "summary_text": "## Synthèse\nTexte.", "structured_data": {"decisions": []}, "language": "fr",
        "speaker_roles": {"SPEAKER_00": {"label": "Personne A"}}, "speaker_count": 1,
    })
    assert "meeting_type" not in ctx and ctx["type_suggere"] == "Réunion projet"


def test_reunion_planifiee_non_validee_reste_jugee_sur_la_suggestion(job, tmp_path):
    """Semis d'une réunion planifiée (titre, date, langue) puis résumé : toujours pas de clé
    meeting_type ⇒ un type suggéré confidentiel rend la fiche confidentielle."""
    confidential_type = sorted(store_mod.confidential_types())[0]
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Planifiée", "date": "2026-06-01", "language": "fr"})
    assert _card(job.id).confidential is False
    fs.save_json(CONTEXT, {"title": "Planifiée", "date": "2026-06-01", "language": "fr",
                           "type_suggere": confidential_type})
    assert _card(job.id).confidential is True


def test_restauration_avec_ancien_manifeste_a_chemin_absolu(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Avant"})
    refine = RefineStore(str(tmp_path), job.id)
    version = refine.snapshot_artifacts([fs.job_dir / CONTEXT])
    fs.save_json(f"refine/versions/v{version}/manifest.json",
                 {"meeting_context.json": str(fs.job_dir / CONTEXT)})     # ancien format : chemin nu, absolu
    fs.save_json(CONTEXT, {"title": "Après"})
    refine.restore_version(version)
    assert _card(job.id).title == "Avant"


# ── Suppression ──────────────────────────────────────────────────────────────

def test_supprimer_le_job_supprime_la_fiche_postgresql(job, tmp_path):
    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"title": "À supprimer"})
    assert _card(job.id) is not None
    assert JobStore.delete_job(job.id) is True
    assert _card(job.id) is None


def test_suppression_sql_brute_cascade_en_base_postgresql(job, tmp_path):
    """Chemin hors ORM (purge, outil) : c'est la clé étrangère ON DELETE CASCADE qui agit."""
    job_id = job.id
    JobFilesystem(str(tmp_path), job_id).save_json(CONTEXT, {"title": "À supprimer"})
    db.session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
    db.session.commit()
    assert _card(job_id) is None


@pytest.fixture
def sqlite_app(tmp_path):
    """App minimale sur SQLite : la seconde base supportée (docs/DISCUTER_REUNIONS.md, lot 0)."""
    sqlite = Flask("fiches-sqlite")
    sqlite.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'fiches.db'}"
    sqlite.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(sqlite)
    import_all_models()
    hooks.register_card_refresh(sqlite)
    with sqlite.app_context():
        db.create_all()
        yield sqlite
        db.session.remove()
        db.drop_all()


def test_sqlite_n_applique_pas_les_cles_etrangeres_la_cascade_orm_supprime_la_fiche(sqlite_app, tmp_path):
    # Constat du lot 0, verrouillé : sans PRAGMA, la cascade EN BASE n'existe pas sur SQLite.
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 0
    user = UserStore.create_user(username="u-sqlite", password="mdp-de-test-sqlite", display_name="U", role=Role.OPERATOR)
    kept = JobStore.create_job(user.id, "Via l'ORM")
    raw = JobStore.create_job(user.id, "Hors ORM")
    for j in (kept, raw):
        JobFilesystem(str(tmp_path), j.id).save_json(CONTEXT, {"title": j.title})
    kept_id, raw_id = kept.id, raw.id
    assert {c.title for c in JobCardStore.for_jobs([kept_id, raw_id]).values()} == {"Via l'ORM", "Hors ORM"}

    assert JobStore.delete_job(kept_id) is True                      # chemin de l'application
    assert _card(kept_id) is None

    db.session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": raw_id})
    db.session.commit()
    assert _card(raw_id) is not None                                 # orpheline : jamais visible (lecture par job visible)
    assert JobCardStore.prune_orphans() == 1
    assert _card(raw_id) is None


# ── Lecture et rattrapage ────────────────────────────────────────────────────

def test_lecture_par_identifiants(job, owner_id, tmp_path):
    other = make_job(owner_id, title="Autre")
    without_card = make_job(owner_id, title="Sans fiche")
    for j in (job, other):
        JobFilesystem(str(tmp_path), j.id).save_json(CONTEXT, {"title": j.title})
    cards = JobCardStore.for_jobs([job.id, other.id, without_card.id, job.id, "inconnu"])
    assert set(cards) == {job.id, other.id}
    assert JobCardStore.for_jobs([]) == {}


def test_rattrapage_reconstruit_puis_ne_reecrit_rien(job, owner_id, tmp_path):
    second = make_job(owner_id, title="Second")
    for j in (job, second):
        JobFilesystem(str(tmp_path), j.id).save_json(CONTEXT, {"title": j.title})
    db.session.execute(db.delete(JobCard).where(JobCard.job_id.in_([job.id, second.id])))
    db.session.commit()

    seen = []
    first = JobCardStore.rebuild_all(str(tmp_path), before_each=seen.append)
    assert first[CREATED] == 2 and first["failed"] == 0
    assert {job.id, second.id} <= set(seen)
    assert _card(job.id).title == "Réunion fiche"

    again = JobCardStore.rebuild_all(str(tmp_path))
    assert (again[CREATED], again[UPDATED], again["failed"]) == (0, 0, 0)
    assert again[UNCHANGED] == 2


def test_rattrapage_recalcule_quand_la_regle_change(job, tmp_path, monkeypatch):
    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"title": "Même fichier"})
    monkeypatch.setattr(store_mod, "source_fingerprint",
                        lambda *a, **kw: "0" * 64)          # projection relevée ⇒ autre empreinte
    assert JobCardStore.rebuild_all(str(tmp_path))[UPDATED] == 1


def test_rattrapage_survit_a_un_job_en_echec(job, owner_id, tmp_path, monkeypatch):
    healthy = make_job(owner_id, title="Sain")
    for j in (job, healthy):
        JobFilesystem(str(tmp_path), j.id).save_json(CONTEXT, {"title": j.title})
    db.session.execute(db.delete(JobCard).where(JobCard.job_id.in_([job.id, healthy.id])))
    db.session.commit()

    def flaky(job_id):
        if job_id == job.id:
            raise OSError("disque en panne")

    counts = JobCardStore.rebuild_all(str(tmp_path), before_each=flaky)
    assert counts["failed"] == 1 and counts[CREATED] == 1
    assert _card(healthy.id) is not None and _card(job.id) is None


def test_rattrapage_au_demarrage_en_tache_de_fond(app, job, tmp_path):
    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"title": "Avant la table"})
    db.session.execute(db.delete(JobCard).where(JobCard.job_id == job.id))
    db.session.commit()
    thread = hooks.start_card_catchup(app, {"storage": {"jobs_dir": str(tmp_path), "shared_backend": "fs"}})
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert _card(job.id).title == "Avant la table"


def test_commande_rebuild_job_cards(app, job, tmp_path, monkeypatch, capsys):
    import app as app_module

    JobFilesystem(str(tmp_path), job.id).save_json(CONTEXT, {"title": "Par la commande"})
    db.session.execute(db.delete(JobCard).where(JobCard.job_id == job.id))
    db.session.commit()
    cfg = {"storage": {"jobs_dir": str(tmp_path), "shared_backend": "fs"}}
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(app_module, "create_app", lambda *a, **kw: app)

    assert cli.main(["rebuild-job-cards"]) == 0
    out = capsys.readouterr().out
    assert "Fiches réunions : 1 créées" in out and "Par la commande" not in out
    assert _card(job.id).title == "Par la commande"


# ── Temps de parole (« qui parle le plus de… ») ──────────────────────────────

def test_les_temps_de_parole_entrent_dans_la_fiche_et_la_suivent(job, tmp_path):
    fs = JobFilesystem(str(tmp_path), job.id)
    fs.save_json(CONTEXT, {"title": "Avec locuteurs"})
    assert _card(job.id).speaking == []
    fs.save_json("speakers/speaker_stats.json", {"speakers": [
        {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 300, "mapped_name": "Personne A"},
        {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 100},
        {"speaker_id": "SPEAKER_02", "speaking_time_seconds": 100},
    ]})
    card = _card(job.id)
    assert card.speaking == [{"name": "Personne A", "pct": 60}] and card.unnamed_speech_pct == 40
    # Le mapping nomme SPEAKER_01 ensuite (éditeur : forme courte, une chaîne nue)
    fs.save_json("speakers/speaker_mapping.json", {"mapping": {"SPEAKER_01": "Personne B"}})
    card = _card(job.id)
    assert card.speaking == [{"name": "Personne A", "pct": 60}, {"name": "Personne B", "pct": 20}]
    assert card.unnamed_speech_pct == 20
    (fs.job_dir / "speakers" / "speaker_stats.json").write_text("{ cassé", encoding="utf-8")
    assert JobCardStore.refresh(job.id, str(tmp_path)) == UPDATED       # fichier illisible : fiche sans temps de parole
    assert _card(job.id).speaking == []


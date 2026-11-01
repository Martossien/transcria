"""Lexique de session : priorités tolérantes, forme à écrire visible, formes ambiguës
signalées, rapport qualité sur la cible attendue (retours utilisateurs 2026-11)."""
from __future__ import annotations

from builders import make_job

from transcria.auth.store import UserStore
from transcria.context.lexicon import LexiconManager, normalize_priority
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState


def test_normalize_priority_tolere_les_synonymes():
    assert normalize_priority("normal") == "normale"
    assert normalize_priority(" Important ") == "importante"
    assert normalize_priority("critical") == "critique"
    assert normalize_priority("n'importe quoi") == "normale"
    assert normalize_priority(None) == "normale"


def test_save_normalise_les_priorites(app):
    with app.app_context():
        job = make_job(UserStore.get_by_username("admin").id, state=JobState.PARTICIPANTS_DONE)
        jobs_dir = app.config.get("TRANSCRIA_JOBS_DIR") or __import__("transcria.config", fromlist=["get_config"]).get_config()["storage"]["jobs_dir"]
        saved = LexiconManager.save(job, jobs_dir, [
            {"term": "Terme A", "priority": "important", "variants": ["terme a"]},
            {"term": "Terme B", "priority": "normal"},
        ])
        assert [e["priority"] for e in saved] == ["importante", "normale"]


def test_etape_lexique_montre_forme_a_ecrire_et_badge_ambigu(app, admin_client):
    with app.app_context():
        job = make_job(UserStore.get_by_username("admin").id, state=JobState.PARTICIPANTS_DONE)
        jobs_dir = __import__("transcria.config", fromlist=["get_config"]).get_config()["storage"]["jobs_dir"]
        JobFilesystem(jobs_dir, job.id).save_json("context/session_lexicon.json", [
            {"id": "t1", "term": "Forme A / Forme B", "category": "mot suspect", "priority": "normale",
             "variants": [], "replace_by": "", "comment": "", "contexts": []},
        ])
        job_id = job.id
    html = admin_client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'type="text" class="form-control form-control-sm lex-replace"' in html
    assert "Forme ambiguë « A / B »" in html
    assert "data-lexicon-help" in html


def test_rapport_qualite_signale_la_cible_absente_apres_une_variante_vue(tmp_path):
    """Variante validée VUE dans le brut, forme attendue absente du corrigé ⇒ signalé ;
    variante jamais vue ⇒ silence ; forme ambiguë « A / B » ⇒ jamais signalée ici."""
    from transcria.jobs.models import Job
    from transcria.quality.quality_report import QualityReporter
    raw = ("1\n00:00:00,000 --> 00:00:04,000\nSPEAKER_00: On valide le projet kubernetes demain.\n\n"
           "2\n00:00:04,000 --> 00:00:08,000\nSPEAKER_01: Le budget passe au comité.\n\n")
    corrected = raw  # la correction n'a rien remplacé
    fs = JobFilesystem(str(tmp_path), "job-q")
    fs.save_text("metadata/transcription.srt", raw)
    fs.save_text("metadata/transcription_corrigee.srt", corrected)
    fs.save_json("metadata/transcription_segments.json", [
        {"start": 0.0, "end": 4.0, "speaker": "Alice", "text": "On valide le projet kubernetes demain."},
        {"start": 4.0, "end": 8.0, "speaker": "Bob", "text": "Le budget passe au comité."},
    ])
    fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 8, "format": "wav", "codec": "pcm",
                                                   "channels": 1, "sample_rate_hz": 16000})
    fs.save_json("context/session_lexicon.json", [
        {"term": "Kubernetes", "category": "technique", "priority": "normale", "variants": ["kubernetes"],
         "replace_by": "Kubernetes (K8s)"},                       # variante vue, cible absente → signalé
        {"term": "Terraform", "category": "technique", "priority": "normale", "variants": ["terraforme"],
         "replace_by": ""},                                      # jamais vue → silence
        {"term": "Budget / Enveloppe", "category": "autre", "priority": "normale", "variants": ["budget"],
         "replace_by": ""},                                      # ambiguë → exclue
    ])
    report = QualityReporter({"storage": {"jobs_dir": str(tmp_path)}}).run_all_checks(
        Job(id="job-q", owner_id="u1", title="T", state=JobState.QUALITY_CHECKING.value))
    missing = [c for c in report["checks"] if c["type"] == "missing_lexicon_terms"]
    assert missing and missing[0]["terms"] == ["Kubernetes (K8s)"]

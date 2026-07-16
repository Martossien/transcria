"""L'étoile polaire (§5.2 du plan qualité) — le pipeline sans Flask, PG ni GPU.

Le pipeline complet (séquencement, checkpoints, provenance, reprise) tourne avec
les builders (``tests/builders``) et les fakes (``tests/fakes``) : aucun serveur
web, aucune base, aucune carte. Ce test verrouille la composition rendue possible
par B2 (coutures injectées du pipeline) + C4 (builders/fakes officiels).

Couture restante assumée : ``JobStore`` est une référence de module dans
``pipeline_service`` (pas un paramètre) — substituée ici par monkeypatch à la
source, comme les tests de phases le font déjà. Son injection complète est
l'affaire du moteur de pipeline (B3).
"""
from builders import make_config, make_job_stub
from fakes import FakeJobStore, FakeWorkflowRunner

from transcria.jobs.models import JobState
from transcria.services import pipeline_service
from transcria.services.pipeline_service import PipelineService

# Séquence attendue du profil `dossier_qualite` (mode legacy « quality ») — la
# table unique de séquencement est verrouillée par les goldens B2 ; on n'affirme
# ici que ce que CE test pilote.
_EXPECTED_CALLS = ["transcription", "diarization", "correction", "final_review", "quality", "export"]


def _make_service(tmp_path, monkeypatch, store):
    cfg = make_config(
        {
            "workflow": {
                "enable_quality_mode": True,
                "arbitration_llm": {"enabled": True},
                # multi_stt est actif dans les défauts du loader (divergence documentée
                # au contrat des vues C3) : coupé ici, la séquence attendue est celle
                # du dossier qualité canonique.
                "multi_stt": {"enabled": False},
                "progress": {"enabled": False},
            },
        },
        jobs_dir=tmp_path / "jobs",
    )
    monkeypatch.setattr(pipeline_service, "JobStore", store)
    svc = PipelineService(cfg)
    svc.runner = FakeWorkflowRunner(cfg)

    # Transforms audio neutralisés à leurs coutures B2 (le préprocess réel — scène,
    # débruitage… — est couvert par ses propres tests ; ici on prouve l'orchestration).
    svc._run_audio_preflight = lambda job, audio_path, sl: {}
    svc._run_audio_scene_analysis = lambda job, audio_path, sl: {}
    svc._refresh_audio_quality_with_scene = lambda job, audio_scene, sl: None
    svc._run_source_separation = lambda job, audio_path, audio_scene, sl: audio_path
    svc._run_audio_scene_filter = lambda job, audio_path, mode, audio_scene, sl: audio_path
    svc._run_audio_denoise = lambda job, audio_path, mode, audio_preflight, sl: audio_path
    svc._run_audio_normalization = lambda job, audio_path, mode, sl, audio_preflight=None: audio_path
    return svc


def test_pipeline_runs_without_flask_pg_gpu(tmp_path, monkeypatch):
    job = make_job_stub()
    store = FakeJobStore(job)
    svc = _make_service(tmp_path, monkeypatch, store)
    audio = tmp_path / "reunion.wav"
    audio.write_bytes(b"RIFF fake wav")

    result = svc.run_process(job, str(audio), mode="quality")

    assert result.get("error") is None
    assert result["status"] == "completed"
    assert svc.runner.calls == _EXPECTED_CALLS
    # Provenance persistée via le store (mécanique de reprise réelle, pas un mock).
    assert store.completed_phases(job.id) == ["preprocess"] + _EXPECTED_CALLS
    assert (job.id, JobState.COMPLETED, None) in store.state_updates


def test_pipeline_resume_skips_all_completed_phases(tmp_path, monkeypatch):
    """Reprise : un second dispatch ne rejoue RIEN (marqueurs + artefacts + empreintes)."""
    job = make_job_stub()
    store = FakeJobStore(job)
    svc = _make_service(tmp_path, monkeypatch, store)
    audio = tmp_path / "reunion.wav"
    audio.write_bytes(b"RIFF fake wav")

    first = svc.run_process(job, str(audio), mode="quality")
    assert first["status"] == "completed"
    calls_after_first = list(svc.runner.calls)

    second = svc.run_process(job, str(audio), mode="quality")

    assert second["status"] == "completed"
    assert svc.runner.calls == calls_after_first  # aucune phase rejouée


def test_pipeline_stops_on_failed_step_without_infra(tmp_path, monkeypatch):
    """Une étape en échec arrête le pipeline, marque FAILED via le store, et les
    étapes aval ne tournent pas."""
    job = make_job_stub()
    store = FakeJobStore(job)
    svc = _make_service(tmp_path, monkeypatch, store)
    audio = tmp_path / "reunion.wav"
    audio.write_bytes(b"RIFF fake wav")

    def failing_correction(j, config):
        svc.runner.calls.append("correction")
        return {"success": False, "error": "LLM indisponible (scripté)"}

    svc.runner.run_correction = failing_correction

    result = svc.run_process(job, str(audio), mode="quality")

    assert result["error"] == "LLM indisponible (scripté)"
    assert result["step"] == "correction"
    assert "final_review" not in svc.runner.calls
    assert "export" not in svc.runner.calls
    assert any(state is JobState.FAILED for _, state, _ in store.state_updates)


def test_end_of_pipeline_llm_stop_skipped_when_another_job_holds_the_lock(tmp_path, monkeypatch):
    """Course arrêt-vs-lancement (campagne de charge B3) : un job qui finit son
    pipeline ne doit PAS arrêter la LLM d'arbitrage qu'un autre job détient (il la
    lançait — SIGTERM en plein chargement, exit 143, vécu en rafale de 3 jobs)."""
    job = make_job_stub()
    store = FakeJobStore(job)
    svc = _make_service(tmp_path, monkeypatch, store)
    svc.runner.vram.arbitrage_running = True

    assert svc.runner.allocator.try_acquire_llm("job-concurrent") is True
    svc._release_arbitrage_llm()

    assert svc.runner.vram.stop_calls == 0                       # arrêt sauté
    assert svc.runner.allocator.owner == "job-concurrent"        # verrou intact
    svc.runner.allocator.release_llm("job-concurrent")


def test_end_of_pipeline_llm_stop_holds_the_lock_while_stopping(tmp_path, monkeypatch):
    """Verrou libre → l'arrêt a lieu SOUS le verrou (aucun lancement concurrent
    possible pendant), puis le verrou est rendu."""
    job = make_job_stub()
    store = FakeJobStore(job)
    svc = _make_service(tmp_path, monkeypatch, store)
    svc.runner.vram.arbitrage_running = True

    svc._release_arbitrage_llm()

    assert svc.runner.vram.stop_calls == 1
    assert svc.runner.allocator.owner is None                    # verrou rendu
    assert svc.runner.allocator.acquire_calls == ["__pipeline_stop__"]

"""Goldens préalables à la vague B1 (extraction des phases du runner).

Figés AVANT tout déplacement de code (plan qualité §B1, « invariants gelés ») :

1. la PROVENANCE de la reprise (`workflow/resume.py`) : ordre des phases, tables de
   déclaration (artefacts non ambigus, entrées empreintées) et empreintes sha256 —
   mêmes entrées ⇒ mêmes empreintes, hex pour hex ;
2. l'ÉMISSION DES NOTIFICATIONS du runner : `notify_summary_ready` part exactement une
   fois sur le succès du résumé, APRÈS la validation SUMMARY_DONE, et jamais sur un
   report VRAM.

Si l'un de ces tests casse pendant B1, l'extraction a changé un invariant de
comportement — c'est un signal d'arrêt, pas un golden à régénérer.
"""
import contextlib
from types import SimpleNamespace

from transcria.workflow import resume


class TestProvenanceTablesFrozen:
    """Les tables de déclaration de resume.py sont le CONTRAT de la reprise."""

    def test_pipeline_phases_order(self):
        assert resume.PIPELINE_PHASES == (
            "preprocess",
            "transcription",
            "diarization",
            "correction",
            "final_review",
            "quality",
            "export",
        )

    def test_phase_artifacts(self):
        assert resume._PHASE_ARTIFACT == {
            "transcription": "metadata/transcription.srt",
            "correction": "metadata/transcription_corrigee.srt",
            "quality": "quality/quality_report.json",
        }

    def test_phase_inputs(self):
        assert resume._PHASE_INPUTS == {
            "preprocess": (),
            "transcription": (),
            "diarization": (),
            "correction": (
                "metadata/transcription.srt",
                "context/session_lexicon_filtered.json",
                "context/job_context.yaml",
            ),
            "final_review": (
                "metadata/transcription_corrigee.srt",
                "context/session_lexicon.json",
            ),
            "quality": (
                "metadata/transcription.srt",
                "metadata/transcription_corrigee.srt",
                "metadata/transcription_segments.json",
                "context/session_lexicon.json",
            ),
            "export": (
                "metadata/transcription.srt",
                "metadata/transcription_corrigee.srt",
                "quality/quality_report.json",
                "context/meeting_context.json",
                "summary/summary.md",
            ),
        }


class TestFingerprintsGolden:
    """Mêmes entrées ⇒ mêmes empreintes (sha256 par contenu, jamais par mtime)."""

    _CONTENT = {
        "metadata/transcription.srt": b"1\n00:00:00,000 --> 00:00:02,000\nBonjour a tous\n\n",
        "context/session_lexicon_filtered.json": b'{"terms": ["TranscrIA"]}\n',
        "context/job_context.yaml": b"meeting_type: reunion\n",
    }
    _EXPECTED = {
        "metadata/transcription.srt": "0685f3e7113da0157c608819827b35290c744e44ece1607a0177d6912989a759",
        "context/session_lexicon_filtered.json": "c71af4f220389060d356f38c81e094bab84ee40ab8f58d71834dedef924f862b",
        "context/job_context.yaml": "02fb8a3779a9e67520d2cb95443ccb13dfe79d62d6f1d67485a5257f76f9bc0d",
    }

    def _fs(self, tmp_path):
        for rel, content in self._CONTENT.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return SimpleNamespace(job_dir=tmp_path)

    def test_correction_fingerprints_exact_hex(self, tmp_path):
        assert resume.compute_input_fingerprints("correction", self._fs(tmp_path)) == self._EXPECTED

    def test_deterministic_across_calls(self, tmp_path):
        fs = self._fs(tmp_path)
        assert resume.compute_input_fingerprints("correction", fs) == resume.compute_input_fingerprints(
            "correction", fs
        )

    def test_absent_input_is_sentinel_not_error(self, tmp_path):
        fingerprints = resume.compute_input_fingerprints("correction", SimpleNamespace(job_dir=tmp_path))
        assert fingerprints == {rel: "absent" for rel in self._CONTENT}

    def test_non_file_path_is_absent_never_an_exception(self, tmp_path):
        # Un chemin qui n'est pas un fichier régulier (ici : un répertoire) vaut
        # « absent » — la sonde ne lève jamais, elle produit toujours une empreinte.
        (tmp_path / "metadata" / "transcription.srt").mkdir(parents=True)
        fingerprints = resume.compute_input_fingerprints("correction", SimpleNamespace(job_dir=tmp_path))
        assert fingerprints["metadata/transcription.srt"] == "absent"

    def test_phases_without_inputs_are_empty(self, tmp_path):
        fs = SimpleNamespace(job_dir=tmp_path)
        for phase in ("preprocess", "transcription", "diarization"):
            assert resume.compute_input_fingerprints(phase, fs) == {}


class TestSummaryNotificationGolden:
    """L'email « pré-analyse prête » : 1 émission, après SUMMARY_DONE, 0 sur report."""

    def test_success_notifies_once_after_summary_done(self, app, owner_id, monkeypatch, tmp_path):
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.workflow.runner import WorkflowRunner

        with app.app_context():
            cfg = {
                "storage": {"jobs_dir": str(tmp_path / "jobs")},
                "workflow": {
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                },
                "services": {
                    "arbitrage_script": "/bin/true",
                    "stop_script": "/bin/true",
                    "arbitrage_llm_port": 8080,
                    "vllm_port": 8000,
                },
                "models": {"cohere_model_path": "/tmp/fake_model"},
            }
            job = JobStore.create_job(owner_id, "Golden notification")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "ensure_free", lambda required_mb: 0)
            monkeypatch.setattr(runner.vram, "untrack_model", lambda name: None)
            monkeypatch.setattr(runner.vram, "offload_all", lambda: None)

            from transcria.stt.summary import SummaryGenerator

            monkeypatch.setattr(
                SummaryGenerator,
                "generate_quick_summary",
                lambda *a, **kw: {
                    "transcript_text": "[0s->5s] Bonjour à tous",
                    "transcript_short": "Bonjour à tous",
                    "summary_text": "Résumé de contrôle indisponible (LLM non configurée).",
                    "segment_count": 1,
                },
            )

            # Import différé dans run_summary → substitution À LA SOURCE (job_facts).
            states_at_call: list[str] = []

            def fake_notify(config, notified_job):
                states_at_call.append(JobStore.get_by_id(notified_job.id).state)

            from transcria.notifications import job_facts

            monkeypatch.setattr(job_facts, "notify_summary_ready", fake_notify)

            audio_path = tmp_path / "test.wav"
            audio_path.write_text("fake")
            result = runner.run_summary(job, str(audio_path), cfg)

            assert result["segment_count"] == 1
            # Exactement UNE notification, émise APRÈS validation du résumé.
            assert states_at_call == [JobState.SUMMARY_DONE.value]

    def test_vram_deferral_does_not_notify(self, app, owner_id, monkeypatch, tmp_path):
        from transcria.gpu.gpu_session import GPUSessionError
        from transcria.jobs.store import JobStore
        from transcria.workflow.runner import WorkflowRunner

        with app.app_context():
            cfg = {
                "storage": {"jobs_dir": str(tmp_path / "jobs")},
                "workflow": {"enable_quick_summary": True, "summary_llm": {"enabled": False}},
                "services": {"arbitrage_script": "/bin/true", "stop_script": "/bin/true"},
                "models": {"cohere_model_path": "/tmp/fake_model"},
            }
            job = JobStore.create_job(owner_id, "Golden report VRAM")
            runner = WorkflowRunner(JobStore, cfg)

            @contextlib.contextmanager
            def fake_gpu_session(job, model_name, required_mb, phase):
                raise GPUSessionError("VRAM insuffisante (simulé)")
                yield  # noqa: unreachable

            monkeypatch.setattr(runner, "_gpu_session", fake_gpu_session)

            calls: list = []
            from transcria.notifications import job_facts

            monkeypatch.setattr(job_facts, "notify_summary_ready", lambda *a: calls.append(a))

            result = runner.run_summary(job, "/tmp/fake.wav", cfg)

            assert result.get("vram_wait") is True
            assert calls == []

"""Tests de la phase CORRECTION (workflow/phases/correction.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
import json  # noqa: F401 — utilisé par les tests de prompting

from transcria.workflow.runner import WorkflowRunner
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.jobs.filesystem import JobFilesystem


def _default_config(**overrides):
    cfg = {
        "storage": {"jobs_dir": "/tmp/test_transcria_jobs"},
        "workflow": {
            "enable_quick_summary": True,
            "enable_speaker_detection": True,
            "enable_quality_mode": True,
            "summary_llm": {"enabled": False},
            "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
        },
        "services": {
            "arbitrage_script": "/bin/true",
            "stop_script": "/bin/true",
            "arbitrage_llm_port": 8080,
            "vllm_port": 8000,
        },
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


class TestWorkflowRunnerRunCorrectionPrompting:
    # NB: renommée le 12/06/2026 — un doublon de nom avec la classe plus bas
    # masquait TOUS ces tests (jamais collectés par pytest).
    def test_run_correction_passes_config_and_keeps_partial_timeout_output(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "arbitration_llm": {"model_id": "local/test-llm-arbitrage", "timeout_seconds": 1234, "opencode_bin": "opencode"},
                },
            )
            job = JobStore.create_job(owner_id, "Correction Partial Timeout")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            from transcria.gpu.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")
            fs.save_text("context/job_context.yaml", "meeting: {}\n")
            fs.save_text("context/session_lexicon.json", "[]\n")

            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)  # pas de réservation VRAM réelle
            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)

            captured = {}

            def fake_run_correction(self, srt_path, context_path, lexicon_path, invite_path=None, **_kw):
                captured["config_timeout"] = self._get_correction_timeout()
                return {
                    "success": True,
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                    "report": "# Rapport\n",
                    "warning": "opencode timeout après 1234s",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            result = runner.run_correction(job, cfg)

            assert result["success"] is True
            assert captured["config_timeout"] == 1234
            assert "corrigé" in fs.load_text("metadata/transcription_corrigee.srt")

    def _correction_setup(self, app, owner_id, monkeypatch, tmp_path, title):
        cfg = _default_config(
            storage={"jobs_dir": str(tmp_path / "jobs")},
            workflow={
                "enable_quick_summary": True, "enable_speaker_detection": True,
                "enable_quality_mode": True, "summary_llm": {"enabled": False},
                "arbitration_llm": {"model_id": "local/test-llm-arbitrage", "opencode_bin": "opencode"},
            },
        )
        job = JobStore.create_job(owner_id, title)
        runner = WorkflowRunner(JobStore, cfg)
        from transcria.jobs.filesystem import JobFilesystem
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")
        fs.save_text("context/job_context.yaml", "meeting: {}\n")
        fs.save_text("context/session_lexicon.json", "[]\n")
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)  # pas de réservation VRAM réelle
        monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
        monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
        return cfg, job, runner, fs

    def test_run_correction_zero_output_retries_then_fails_loud(self, app, owner_id, monkeypatch, tmp_path):
        """opencode exit 0 sans rien produire (famille e62295c1, vu avec Ministral 14B
        le 12/06/2026) : AVANT, l'étape était validée en silence (SRT brut servi comme
        corrigé). Désormais : retry ≤ 3 puis échec EXPLICITE relançable."""
        with app.app_context():
            cfg, job, runner, fs = self._correction_setup(app, owner_id, monkeypatch, tmp_path, "Correction 0 texte")
            from transcria.gpu.opencode_runner import OpenCodeRunner
            calls = {"n": 0}

            def fake_run_correction(self, srt_path, context_path, lexicon_path, invite_path=None, **_kw):
                calls["n"] += 1
                return {"success": True, "corrected_srt": "", "report": "", "error": ""}

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)
            result = runner.run_correction(job, cfg)

            assert calls["n"] == 3  # retries (LLM déjà chargée : passes LLM seulement)
            assert result["success"] is False
            assert "aucune correction" in result["error"]
            assert fs.load_text("metadata/transcription_corrigee.srt") is None  # rien de faux publié

    def test_run_correction_recovers_on_second_attempt(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg, job, runner, fs = self._correction_setup(app, owner_id, monkeypatch, tmp_path, "Correction retry OK")
            from transcria.gpu.opencode_runner import OpenCodeRunner
            calls = {"n": 0}

            def fake_run_correction(self, srt_path, context_path, lexicon_path, invite_path=None, **_kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"success": True, "corrected_srt": "", "report": "", "error": ""}
                return {"success": True, "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                        "report": "", "error": ""}

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)
            result = runner.run_correction(job, cfg)

            assert calls["n"] == 2
            assert result["success"] is True
            assert "corrigé" in fs.load_text("metadata/transcription_corrigee.srt")

    def test_run_correction_filters_session_lexicon_before_llm(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(
                storage={"jobs_dir": str(tmp_path / "jobs")},
                workflow={
                    "enable_quick_summary": True,
                    "enable_speaker_detection": True,
                    "enable_quality_mode": True,
                    "summary_llm": {"enabled": False},
                    "arbitration_llm": {"model_id": "local/test-llm-arbitrage"},
                },
            )
            job = JobStore.create_job(owner_id, "Correction Lexicon Filter")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.gpu.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nLe denes répond à l'API.\n")
            fs.save_text("context/job_context.yaml", "meeting: {}\n")
            fs.save_json("context/session_lexicon.json", [
                {"term": "DNS", "variants": ["dénès"], "priority": "normale"},
                {"term": "API", "variants": [], "priority": "normale"},
                {"term": "SI critique", "variants": [], "priority": "critique"},
                {"term": "Absent normal", "variants": [], "priority": "normale"},
            ])

            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)  # pas de réservation VRAM réelle
            captured = {}

            def fake_run_correction(self, srt_path, context_path, lexicon_path, invite_path=None, **_kw):
                captured["lexicon_path"] = lexicon_path
                with open(lexicon_path, "r", encoding="utf-8") as fh:
                    captured["lexicon"] = json.load(fh)
                return {
                    "success": True,
                    # SRT corrigé STRUCTURELLEMENT valide : la garde d'intégrité exige
                    # la parité des segments avec le source (1 timecode ici).
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nLe DNS répond à l'API.\n",
                    "report": "",
                    "warning": "",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            result = runner.run_correction(job, cfg)

            assert result["success"] is True
            assert captured["lexicon_path"].endswith("session_lexicon_filtered.json")
            assert [entry["term"] for entry in captured["lexicon"]] == ["DNS", "API", "SI critique"]
            assert captured["lexicon"][2]["_preservation_only"] is True
            assert fs.load_json("context/session_lexicon.json")[3]["term"] == "Absent normal"


class TestWorkflowRunnerRunCorrection:
    def test_run_correction_success(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction OK")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner.vram, "free_all_gpus", lambda: True)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.gpu.opencode_runner import OpenCodeRunner

            def fake_run_correction(self_runner, srt_path, context_path, lexicon_path, invite_path=None, **_kw):
                return {
                    "success": True,
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                    "report": "# Rapport de correction\n2 corrections appliquées",
                    "error": "",
                }

            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            from transcria.jobs.filesystem import JobFilesystem

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")

            result = runner.run_correction(job, cfg)
            assert result["success"] is True
            assert "corrigé" in result["corrected_srt"]

            saved_srt = fs.load_text("metadata/transcription_corrigee.srt")
            assert saved_srt is not None
            assert "corrigé" in saved_srt

    def test_run_correction_llm_not_available(self, app, owner_id, monkeypatch, tmp_path):
        """ensure_arbitrage_llm_ready retourne False → erreur claire, sans dépendance au port 8080."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No LLM")
            runner = WorkflowRunner(JobStore, cfg)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            # Patcher directement ensure_arbitrage_llm_ready évite la dépendance
            # au port 8080 réel (CAS A contourne launch_arbitrage_llm).
            # _should_reserve_llm_vram est désactivé : pas de GPU réel dans ce test.
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(
                runner.vram,
                "ensure_arbitrage_llm_ready",
                lambda expected_model_id=None: False,
            )

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "non disponible" in result["error"]

    def test_run_correction_missing_srt(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction No SRT")
            runner = WorkflowRunner(JobStore, cfg)

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "SRT" in result["error"]

    def test_run_correction_exception_stops_arbitrage_llm(self, app, owner_id, monkeypatch, tmp_path):
        """Si la LLM a été lancée par ce call (CAS C) et que opencode plante, elle doit être stoppée."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction Crash")
            runner = WorkflowRunner(JobStore, cfg)

            # Simule CAS C : LLM absente avant l'appel, lancée avec succès par ensure_…
            # _should_reserve_llm_vram est désactivé : pas de GPU réel dans ce test.
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: False)
            monkeypatch.setattr(
                runner.vram,
                "ensure_arbitrage_llm_ready",
                lambda expected_model_id=None: True,
            )

            stop_called = {"v": False}
            def fake_stop():
                stop_called["v"] = True
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", fake_stop)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            monkeypatch.setattr(
                OpenCodeRunner,
                "run_correction",
                lambda self, s, c, l: (_ for _ in ()).throw(RuntimeError("LLM crash")),
            )

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert stop_called["v"] is True, "stop_arbitrage_llm doit être appelé quand la LLM a été lancée par ce call"

    def test_run_correction_exception_does_not_stop_preexisting_llm(self, app, owner_id, monkeypatch, tmp_path):
        """CAS A : si la LLM tournait déjà avant l'appel, une exception ne doit PAS la stopper."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction Crash CAS-A")
            runner = WorkflowRunner(JobStore, cfg)

            # Simule CAS A : LLM déjà active avant l'appel.
            # _should_reserve_llm_vram est désactivé : pas de GPU réel dans ce test.
            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(
                runner.vram,
                "ensure_arbitrage_llm_ready",
                lambda expected_model_id=None: True,
            )

            stop_called = {"v": False}
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: stop_called.__setitem__("v", True))

            from transcria.gpu.opencode_runner import OpenCodeRunner
            monkeypatch.setattr(
                OpenCodeRunner,
                "run_correction",
                lambda self, s, c, l: (_ for _ in ()).throw(RuntimeError("LLM crash")),
            )

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nTest\n")

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert stop_called["v"] is False, "stop_arbitrage_llm ne doit PAS être appelé si la LLM était déjà active"


class TestCorrectedSrtIntegrityGuard:
    """Garde déterministe du contrat de correction : le prompt exige (parité des
    segments, ratio anti-résumé), le code vérifie — un SRT tronqué ou réécrit ne
    passe plus avec un simple « non vide »."""

    def _src(self, n_segments: int, line: str = "SPEAKER_00(Alice): Bonjour à tous.") -> str:
        return "".join(
            f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{line}\n\n"
            for i in range(1, n_segments + 1)
        )

    def test_conforme_passe(self):
        src = self._src(50)
        assert WorkflowRunner._corrected_srt_integrity_error(src, src) is None

    def test_segments_perdus_detectes(self):
        src = self._src(50)
        truncated = self._src(25)
        err = WorkflowRunner._corrected_srt_integrity_error(src, truncated)
        assert err is not None and "25 segments au lieu de 50" in err

    def test_reecriture_prefixes_locuteurs_detectee(self):
        """Cas réel (Ministral, job 4bda98cb) : préfixes `SPEAKER_XX(Nom):` réécrits
        en `Nom:` — même nombre de segments mais ratio de taille hors fenêtre."""
        src = self._src(60)
        rewritten = self._src(60, line="Alice: Bonjour à tous.")
        err = WorkflowRunner._corrected_srt_integrity_error(src, rewritten)
        assert err is not None and "ratio" in err

    def test_petit_srt_exempt_du_ratio(self):
        """Sur un SRT minuscule, une correction d'un mot fait varier le ratio sans
        signal : seul le compte de segments est exigé."""
        src = "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n"
        corrected = "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé et complété\n"
        assert WorkflowRunner._corrected_srt_integrity_error(src, corrected) is None

    def test_run_correction_refuse_un_corrige_tronque(self, app, owner_id, monkeypatch, tmp_path):
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Correction tronquée")
            runner = WorkflowRunner(JobStore, cfg)

            monkeypatch.setattr(runner, "_should_reserve_llm_vram", lambda: False)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            src = self._src(40)
            fs.save_text("metadata/transcription.srt", src)

            from transcria.gpu.opencode_runner import OpenCodeRunner
            truncated = self._src(10)
            monkeypatch.setattr(
                OpenCodeRunner, "run_correction",
                lambda self_r, s, c, lx, invite_path=None, **_kw: {"success": True, "corrected_srt": truncated, "report": "", "error": ""},
            )

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "10 segments au lieu de 40" in result["error"]
            assert fs.load_text("metadata/transcription_corrigee.srt") is None  # rien d'écrit

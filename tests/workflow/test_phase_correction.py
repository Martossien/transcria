"""Tests de la phase CORRECTION (workflow/phases/correction.py) — migrés de test_workflow_runner.py (B1 lot 2)."""
import json  # noqa: F401 — utilisé par les tests de prompting

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.workflow.runner import WorkflowRunner


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
            from transcria.llm_tools.opencode_runner import OpenCodeRunner

            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")
            fs.save_text("context/job_context.yaml", "meeting: {}\n")
            fs.save_text("context/session_lexicon.json", "[]\n")

            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)  # pas de réservation VRAM réelle
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
        monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
        return cfg, job, runner, fs

    def test_run_correction_zero_output_retries_then_fails_loud(self, app, owner_id, monkeypatch, tmp_path):
        """opencode exit 0 sans rien produire (famille e62295c1, vu avec Ministral 14B
        le 12/06/2026) : AVANT, l'étape était validée en silence (SRT brut servi comme
        corrigé). Désormais : retry ≤ 3 puis échec EXPLICITE relançable."""
        with app.app_context():
            cfg, job, runner, fs = self._correction_setup(app, owner_id, monkeypatch, tmp_path, "Correction 0 texte")
            from transcria.llm_tools.opencode_runner import OpenCodeRunner
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
            from transcria.llm_tools.opencode_runner import OpenCodeRunner
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

            from transcria.llm_tools.opencode_runner import OpenCodeRunner

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
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready", lambda expected_model_id=None: True)

            from transcria.llm_tools.opencode_runner import OpenCodeRunner

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

            from transcria.llm_tools.opencode_runner import OpenCodeRunner
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

            from transcria.llm_tools.opencode_runner import OpenCodeRunner
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

            from transcria.llm_tools.opencode_runner import OpenCodeRunner
            truncated = self._src(10)
            monkeypatch.setattr(
                OpenCodeRunner, "run_correction",
                lambda self_r, s, c, lx, invite_path=None, **_kw: {"success": True, "corrected_srt": truncated, "report": "", "error": ""},
            )

            result = runner.run_correction(job, cfg)
            assert result["success"] is False
            assert "10 segments au lieu de 40" in result["error"]
            assert fs.load_text("metadata/transcription_corrigee.srt") is None  # rien d'écrit


class TestCorrectionRetryRelanceLaLlm:
    """La boucle de retry RE-VÉRIFIE la LLM avant chaque nouvel essai (miroir du résumé).

    Vécu au gate E2E du 2026-08-03 : serveur d'arbitrage tombé EN PLEINE session de
    correction — sans relance, les tentatives suivantes butaient sur la pré-garde TCP en
    ~10 s chacune et la phase échouait, alors qu'un serveur relancé suffisait.
    """

    def test_gel_declenche_re_verification_llm(self):
        from transcria.workflow.phases.correction import _invoke_correction_with_retries

        calls = {"runs": 0, "ensures": 0}

        class _FakeOcr:
            def run_correction(self, *a, **k):
                calls["runs"] += 1
                if calls["runs"] == 1:
                    return {"success": False, "corrected_srt": "", "report": "",
                            "error": "opencode interrompu (gel détecté)"}
                return {"success": True, "corrected_srt": "1\n00:00:00,000 --> 00:00:01,000\nok\n",
                        "report": "", "error": ""}

        class _FakeVram:
            def ensure_arbitrage_llm_ready(self, expected_model_id=None):
                calls["ensures"] += 1
                return True

        class _FakeRunner:
            vram = _FakeVram()

        class _FakeJob:
            def get_extra_data(self):
                return {}

        result = _invoke_correction_with_retries(
            _FakeOcr(), _FakeJob(), staged_srt="s", staged_context="c",
            staged_lexicon="l", staged_invite=None,
            runner=_FakeRunner(), api_model_id="local/x")

        assert calls["runs"] == 2 and calls["ensures"] == 1
        assert result["success"] is True and result["corrected_srt"]

    def test_sans_runner_le_comportement_historique_est_preserve(self):
        # Les appels existants sans `runner` (tests historiques, autres chemins) ne
        # doivent pas casser : la boucle retente simplement sans re-vérification.
        from transcria.workflow.phases.correction import _invoke_correction_with_retries

        runs = {"n": 0}

        class _FakeOcr:
            def run_correction(self, *a, **k):
                runs["n"] += 1
                return {"success": False, "corrected_srt": "", "report": "",
                        "error": "opencode interrompu (gel)"}

        class _FakeJob:
            def get_extra_data(self):
                return {}

        result = _invoke_correction_with_retries(
            _FakeOcr(), _FakeJob(), staged_srt="s", staged_context="c",
            staged_lexicon="l", staged_invite=None)
        assert runs["n"] == 3 and result["success"] is False


class TestContexteReprojetteAvantStaging:
    def test_les_hints_frais_atteignent_la_correction(self, app, owner_id, monkeypatch, tmp_path):
        """Vécu (audit 2026-08-04) : le contexte n'était rebâti que par les endpoints
        utilisateur — des hints de fiabilité écrits APRÈS le dernier build n'atteignaient
        jamais la LLM (`segments: []` stagé). La phase reprojette désormais le contexte."""
        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Contexte frais")
            runner = WorkflowRunner(JobStore, cfg)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready",
                                lambda expected_model_id=None: True)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt",
                         "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")
            # Contexte PÉRIMÉ sur disque (comme après le dernier endpoint utilisateur)…
            fs.save_text("context/job_context.yaml", "quality_hints:\n  segments: []\n")
            # …alors que la transcription a depuis écrit un segment flagué.
            fs.save_json("metadata/transcription_segments.json", [{
                "start": 3.0, "end": 3.4, "text": "Tenez !",
                "reliability": "suspect", "reliability_reasons": ["segment_court"],
            }])

            staged_yaml: dict = {}
            from transcria.llm_tools.opencode_runner import OpenCodeRunner

            def fake_run_correction(self_runner, srt_path, context_path, lexicon_path,
                                    invite_path=None, **_kw):
                from pathlib import Path
                staged_yaml["content"] = Path(context_path).read_text(encoding="utf-8")
                return {"success": True,
                        "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n",
                        "report": "# ok", "error": ""}
            monkeypatch.setattr(OpenCodeRunner, "run_correction", fake_run_correction)

            result = runner.run_correction(job, cfg)
            assert result["success"] is True
            # La LLM a vu le hint (fichier STAGÉ, pas seulement le canonique).
            assert "segment_court" in staged_yaml["content"]
            assert "Tenez" in staged_yaml["content"]

    def test_la_reprojection_precede_la_surveillance_du_workspace(
            self, app, owner_id, monkeypatch, tmp_path, caplog):
        """Vécu 2026-08-05 : la reprojection tournait APRÈS la capture des empreintes
        de surveillance de l'AgentWorkspace — CHAQUE job accusait l'agent d'avoir
        altéré context/job_context.json (ERROR mensongère). Les écritures canoniques
        de préparation précèdent désormais la création du workspace : un run nominal
        ne déclenche plus aucune alerte « canonique altéré »."""
        import logging

        with app.app_context():
            cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
            job = JobStore.create_job(owner_id, "Reprojection avant surveillance")
            runner = WorkflowRunner(JobStore, cfg)
            monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
            monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
            monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready",
                                lambda expected_model_id=None: True)

            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
            fs.save_text("metadata/transcription.srt",
                         "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")

            from transcria.llm_tools.opencode_runner import OpenCodeRunner
            monkeypatch.setattr(
                OpenCodeRunner, "run_correction",
                lambda self, srt, ctx, lex, invite_path=None, **_kw: {
                    "success": True,
                    "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n",
                    "report": "# ok", "error": ""})

            with caplog.at_level(logging.ERROR, logger="transcria.workflow.agent_workspace"):
                result = runner.run_correction(job, cfg)

            assert result["success"] is True
            altered = [r for r in caplog.records if "altéré" in r.getMessage()]
            assert not altered, f"alerte(s) mensongère(s) : {[r.getMessage() for r in altered]}"


def test_run_correction_srt_vide_echoue_sans_llm(app, owner_id, monkeypatch, tmp_path):
    """SRT vide = rien à corriger : constat immédiat, AUCUNE tentative LLM
    (vérité terrain bruit blanc : 3 tentatives pour rien puis exception)."""
    with app.app_context():
        cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
        job = JobStore.create_job(owner_id, "SRT vide")
        runner = WorkflowRunner(JobStore, cfg)
        llm_touched = {}
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready",
                            lambda expected_model_id=None: llm_touched.setdefault("hit", True))

        from transcria.jobs.filesystem import JobFilesystem
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt", "   \n")

        result = runner.run_correction(job, cfg)
        assert result["success"] is False
        assert "vide" in result["error"]
        assert "hit" not in llm_touched


def test_rapport_de_repli_quand_l_agent_n_en_rend_pas(app, owner_id, monkeypatch, tmp_path):
    """L'agent peut (rarement, vécu 2 fois le 2026-08-04) ne pas rendre
    correction_report.md : un rapport de repli DIFF est généré — l'utilisateur a
    toujours un artefact, et le WARNING rend l'occurrence visible."""
    with app.app_context():
        cfg = _default_config(storage={"jobs_dir": str(tmp_path / "jobs")})
        job = JobStore.create_job(owner_id, "Correction sans rapport")
        runner = WorkflowRunner(JobStore, cfg)
        monkeypatch.setattr(runner.vram, "launch_arbitrage_llm", lambda: True)
        monkeypatch.setattr(runner.vram, "stop_arbitrage_llm", lambda: True)
        monkeypatch.setattr(runner.vram, "is_arbitrage_llm_running", lambda: True)
        monkeypatch.setattr(runner.vram, "ensure_arbitrage_llm_ready",
                            lambda expected_model_id=None: True)

        from transcria.llm_tools.opencode_runner import OpenCodeRunner
        monkeypatch.setattr(OpenCodeRunner, "run_correction",
                            lambda self, srt, ctx, lex, invite_path=None, **_kw: {
                                "success": True,
                                "corrected_srt": "1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n",
                                "report": "",  # l'agent n'a rien rendu
                                "error": ""})

        from transcria.jobs.filesystem import JobFilesystem
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt",
                     "1\n00:00:00,000 --> 00:00:05,000\nBonjour\n")

        result = runner.run_correction(job, cfg)
        assert result["success"] is True
        report = fs.load_text("metadata/correction_report.md")
        assert report and "repli système" in report
        assert "avant : Bonjour" in report and "après : Bonjour corrigé" in report


class TestRelaisDeLecture:
    """Les deux relais nés du banc 2026-08-23 : seule la LECTURE attrapait les dégâts
    de la correction — le code met désormais sous les yeux ce qui doit être lu."""

    def _srt(self, *textes):
        return "\n\n".join(
            f"{i}\n00:0{i}:00,000 --> 00:0{i}:04,000\nSPEAKER_00(Alice): {txt}"
            for i, txt in enumerate(textes, 1)
        )

    def test_l_annexe_diff_est_ajoutee_au_rapport_de_l_agent(self, tmp_path):
        """Le rapport de l'agent est une auto-déclaration ; l'annexe est la vérité
        terrain. L'utilisateur voit immédiatement ce que l'agent a tu."""
        from transcria.workflow.phases.correction import _annexe_diff

        source = self._srt("bonjour a tous", "rien à dire")
        corrige = self._srt("bonjour à tous", "rien à dire")

        annexe = _annexe_diff(source, corrige, "fr")

        assert "## Annexe — diff factuel" in annexe
        assert "1 ligne(s) modifiée(s)" in annexe
        assert "bonjour a tous" in annexe and "bonjour à tous" in annexe

    def test_l_annexe_existe_dans_les_cinq_langues(self):
        from transcria.workflow.phases.correction import _MSG

        for lang, table in _MSG.items():
            assert "diff_annex_title" in table, lang
            assert "{n}" in table["diff_annex_intro"], lang

    def _prepare(self, tmp_path, source, corrige):
        from transcria.workflow.phases.correction import _flag_heavy_rewrites

        fs = JobFilesystem(str(tmp_path), "j-relais")
        n = len(source.strip().split("\n\n"))
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": float(i), "end": i + 4.0, "text": "x"} for i in range(n)])
        _flag_heavy_rewrites(fs, source, corrige)
        return fs.load_json("metadata/transcription_segments.json")

    def test_un_segment_fortement_reecrit_devient_suspect(self, tmp_path):
        source = self._srt("On avait un outil qui est Machin, qui est un produit national", "ok")
        corrige = self._srt("On avait un outil qui est un produit national", "ok")

        segments = self._prepare(tmp_path, source, corrige)

        assert segments[0]["reliability"] == "suspect"
        assert "correction_lourde" in segments[0]["reliability_reasons"]
        assert "reliability" not in segments[1] or segments[1].get("reliability") != "suspect"

    def test_une_petite_correction_ne_declenche_rien(self, tmp_path):
        """Accent, casse, un mot : signaler tout reviendrait à ne rien signaler."""
        source = self._srt("bonjour a tous les collegues presents aujourd'hui")
        corrige = self._srt("bonjour à tous les collègues présents aujourd'hui")

        segments = self._prepare(tmp_path, source, corrige)

        assert segments[0].get("reliability") != "suspect"

    def test_un_segment_marque_incertain_n_est_pas_double(self, tmp_path):
        """[INCERTAIN]/[ÉTRANGER] : l'éditeur les signale déjà par le texte même."""
        source = self._srt("une phrase entière qui va être remplacée par un marqueur")
        corrige = self._srt("une phrase [INCERTAIN: hallucination probable]")

        segments = self._prepare(tmp_path, source, corrige)

        assert segments[0].get("reliability") != "suspect"

    def test_sans_alignement_1_1_on_n_ecrit_rien(self, tmp_path):
        """Un drapeau posé sur le mauvais segment serait pire que pas de drapeau."""
        from transcria.workflow.phases.correction import _flag_heavy_rewrites

        fs = JobFilesystem(str(tmp_path), "j-relais")
        fs.save_json("metadata/transcription_segments.json", [{"start": 0.0, "end": 4.0}])
        source = self._srt("un texte assez long pour être flaggé sans hésitation", "deux")
        corrige = self._srt("court", "deux")

        _flag_heavy_rewrites(fs, source, corrige)

        assert "reliability" not in fs.load_json("metadata/transcription_segments.json")[0]

    def test_un_niveau_degrade_n_est_pas_retrograde(self, tmp_path):
        from transcria.workflow.phases.correction import _flag_heavy_rewrites

        fs = JobFilesystem(str(tmp_path), "j-relais")
        fs.save_json("metadata/transcription_segments.json",
                     [{"start": 0.0, "end": 4.0, "reliability": "degrade",
                       "reliability_reasons": ["audio_preflight_degrade"]}])
        source = self._srt("un long segment qui sera très fortement réécrit par l'agent")
        corrige = self._srt("réécrit")

        _flag_heavy_rewrites(fs, source, corrige)

        seg = fs.load_json("metadata/transcription_segments.json")[0]
        assert seg["reliability"] == "degrade"
        assert "correction_lourde" in seg["reliability_reasons"]

"""Phase ``refine`` (chat d'affinage des livrables) — GPU-free, LLM/opencode mockés.

Contrat : best-effort intégral, verrou LLM + réservation VRAM, conversation persistée
dans ``refine/chat.json``. Le mode ``discuss`` est un appel LLM DIRECT
(``refine_llm.chat_completion`` mocké) ; le mode ``apply`` passe par opencode
(``AgentWorkspace`` isolé, garde-fous déterministes en sortie — intégrité SRT, JSON
valides, options de rendu filtrées — snapshot de version AVANT tout write-back).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcria.workflow.refine_store import RefineStore
from transcria.workflow.runner import WorkflowRunner

_SRT = "".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\nSPEAKER_00(Alice): Segment numéro {i} de la réunion.\n\n"
    for i in range(1, 4)
)


# ── Fakes infra (allocator / vram / progress) ─────────────────────────────────

class _FakeAllocator:
    def __init__(self, llm_available: bool = True):
        self.llm_available = llm_available
        self.released = []

    def try_acquire_llm(self, job_id, timeout_s=0):
        return self.llm_available

    def get_gpu_info(self):
        return [{"id": 0}]

    def try_reserve(self, job_id, mb, phase):
        return SimpleNamespace(gpu_index=0)

    def try_reserve_llm(self, job_id, total_mb, phase):
        return True

    def release_phase(self, job_id, phase):
        self.released.append(("phase", phase))

    def release_llm(self, job_id):
        self.released.append(("llm", job_id))


class _FakeVram:
    def __init__(self, ready: bool = True):
        self.ready = ready

    def is_arbitrage_llm_running(self):
        return True  # déjà chargée → pas de stop en fin de phase

    def ensure_arbitrage_llm_ready(self, expected_model_id=None):
        return self.ready

    def stop_arbitrage_llm(self):
        pass


class _FakeProgress:
    def update(self, *a, **k):
        pass

    def clear(self, *a, **k):
        pass


class _FakeOpenCode:
    """Simule OpenCodeRunner : écrit des fichiers de sortie dans le scratch."""

    outputs: dict[str, str] = {}          # nom → contenu (classe : configuré par test)
    seen: dict = {}

    def __init__(self, work_dir, opencode_bin=None, config=None):
        self.work_dir = Path(work_dir)

    def run_refine(self, **kwargs):
        _FakeOpenCode.seen = dict(kwargs)
        # Capture du contexte conversationnel AU MOMENT du run (le scratch est purgé après).
        conv = Path(kwargs.get("conversation_path", ""))
        _FakeOpenCode.seen["conversation_text"] = conv.read_text(encoding="utf-8") if conv.is_file() else ""
        for name, content in _FakeOpenCode.outputs.items():
            (self.work_dir / name).write_text(content, encoding="utf-8")
        return {"success": True}


class _FakeChat:
    """Simule refine_llm.chat_completion (appel direct du mode discuss)."""

    answer: str = ""
    seen: dict = {}

    @classmethod
    def call(cls, config, messages, **kwargs):
        cls.seen = {"messages": messages, **kwargs}
        return cls.answer


# ── Environnement de test ─────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    config = {
        "storage": {"jobs_dir": str(jobs_dir), "agent_work_dir": str(tmp_path / "agent-work")},
        "workflow": {"refine_chat": {"enabled": True}, "arbitration_llm": {}},
        "gpu": {"llm_vram_mb": 1000},
    }
    from transcria.jobs.filesystem import JobFilesystem

    fs = JobFilesystem(str(jobs_dir), "j1")
    fs.save_json("context/meeting_context.json", {
        "title": "Réunion test", "summary_llm": "Synthèse initiale de la réunion.",
        "structured_data": {"decisions": ["Décision A"]},
    })
    fs.save_text("metadata/transcription_corrigee.srt", _SRT)
    fs.save_json("quality/review_points.json", ["Variantes lexique non résolues : Téhou / TU."])

    runner = WorkflowRunner(object, config)
    runner.allocator = _FakeAllocator()
    runner.vram = _FakeVram()
    runner.progress = _FakeProgress()

    # C5 : les phases importent OpenCodeRunner et chat_completion en tête — patcher les consommateurs
    # (refine pour discuss/apply, final_review pour l'extraction des champs de type).
    import transcria.workflow.phases.final_review as final_review_mod
    import transcria.workflow.phases.refine as refine_mod
    monkeypatch.setattr(refine_mod, "OpenCodeRunner", _FakeOpenCode)
    _FakeOpenCode.outputs, _FakeOpenCode.seen = {}, {}

    monkeypatch.setattr(refine_mod, "chat_completion", _FakeChat.call)
    monkeypatch.setattr(final_review_mod, "chat_completion", _FakeChat.call)
    _FakeChat.answer, _FakeChat.seen = "", {}

    job = SimpleNamespace(id="j1", title="Réunion test")
    store = RefineStore(jobs_dir=str(jobs_dir), job_id="j1")
    return SimpleNamespace(runner=runner, config=config, fs=fs, job=job, store=store)


def _srt_of(env) -> str:
    return env.fs.load_text("metadata/transcription_corrigee.srt") or ""


# ── Mode discussion ───────────────────────────────────────────────────────────

class TestRunRefineDiscuss:
    def test_discuss_appends_answer_no_file_change(self, env):
        env.store.write_request(kind="discuss", message="De quoi parle la réunion ?")
        _FakeChat.answer = "La réunion porte sur…\n---\nProposition d'application : aucune"
        before = _srt_of(env)

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        turns = env.store.load_turns()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert "porte sur" in turns[1]["text"]
        assert _srt_of(env) == before                      # aucun artefact modifié
        assert env.store.has_active_request() is False     # demande consommée
        assert _FakeOpenCode.seen == {}                    # AUCUN run opencode (appel direct)

    def test_discuss_proposal_extracted_into_turn(self, env):
        env.store.write_request(kind="discuss", message="Peut-on raccourcir ?")
        _FakeChat.answer = (
            "Oui, la synthèse peut être condensée.\n\n---\n"
            "Proposition d'application : raccourcir la synthèse de moitié."
        )

        env.runner.run_refine(env.job, env.config)

        turn = env.store.load_turns()[-1]
        assert turn["proposal"] == "raccourcir la synthèse de moitié."
        assert "Proposition" not in turn["text"]     # bloc retiré du texte affiché

    def test_discuss_history_replayed_as_chat_turns(self, env):
        # Les tours précédents sont rejoués en VRAIS messages user/assistant,
        # la demande courante en dernier.
        env.store.append_turn(role="user", kind="discuss", text="Premier échange ?")
        env.store.append_turn(role="assistant", kind="discuss", text="Réponse initiale mémorable.")
        env.store.write_request(kind="discuss", message="Et ensuite ?")
        _FakeChat.answer = "Suite."

        env.runner.run_refine(env.job, env.config)

        messages = _FakeChat.seen["messages"]
        assert messages[0]["role"] == "system"
        assert [m["role"] for m in messages[1:]] == ["user", "assistant", "user"]
        assert "mémorable" in messages[2]["content"]
        assert messages[-1]["content"] == "Et ensuite ?"

    def test_discuss_context_includes_srt_and_review_points(self, env):
        # Le message système embarque la transcription ET les points du contrôle
        # qualité (dont « Variantes lexique non résolues »).
        env.store.write_request(kind="discuss", message="Des points à corriger ?")
        _FakeChat.answer = "Oui."

        env.runner.run_refine(env.job, env.config)

        system = _FakeChat.seen["messages"][0]["content"]
        assert "Segment numéro 1" in system                       # SRT inline
        assert "Variantes lexique non résolues" in system         # points qualité

    def test_no_request_is_noop(self, env):
        result = env.runner.run_refine(env.job, env.config)
        assert result["success"] is True and result.get("skipped") is True
        assert env.store.load_turns() == []

    def test_llm_busy_gives_feedback_turn(self, env):
        env.runner.allocator = _FakeAllocator(llm_available=False)
        env.store.write_request(kind="discuss", message="Occupé ?")

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        turns = env.store.load_turns()
        assert turns[-1]["role"] == "assistant" and "occupé" in turns[-1]["text"].lower()
        assert env.store.has_active_request() is False     # pas de demande fantôme bloquante

    def test_disabled_feature_skips(self, env):
        env.config["workflow"]["refine_chat"]["enabled"] = False
        env.store.write_request(kind="discuss", message="?")
        result = env.runner.run_refine(env.job, env.config)
        assert result.get("skipped") is True

    def test_llm_call_failure_gives_feedback_turn(self, env, monkeypatch):
        env.store.write_request(kind="discuss", message="?")

        import transcria.workflow.phases.refine as refine_mod

        def _boom(config, messages, **kwargs):
            raise RuntimeError("LLM injoignable")

        monkeypatch.setattr(refine_mod, "chat_completion", _boom)
        result = env.runner.run_refine(env.job, env.config)
        assert result["success"] is True                   # best-effort
        assert env.store.load_turns()[-1]["role"] == "assistant"

    def test_apply_opencode_failure_gives_feedback_turn(self, env):
        env.store.write_request(kind="apply", message="Fais X")

        class _Boom(_FakeOpenCode):
            def run_refine(self, **kwargs):
                raise RuntimeError("opencode indisponible")

        import transcria.workflow.phases.refine as refine_mod
        refine_mod.OpenCodeRunner = _Boom
        result = env.runner.run_refine(env.job, env.config)
        assert result["success"] is True                   # best-effort
        assert env.store.load_turns()[-1]["role"] == "assistant"


# ── Mode application ──────────────────────────────────────────────────────────

class TestRunRefineApply:
    def test_apply_writes_back_summary_and_options_with_version(self, env, monkeypatch):
        rebuilt = []
        import transcria.exports.package_builder as pb
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: rebuilt.append(job.id) or {})

        env.store.write_request(kind="apply", message="Raccourcis la synthèse et masque la transcription")
        _FakeOpenCode.outputs = {
            "summary_refined.md": "Synthèse raccourcie.",
            "render_options_refined.json": json.dumps({"sections": {"transcript": False}, "autre": "junk"}),
            "refine_report.md": "Synthèse raccourcie ; transcription masquée.",
        }

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        ctx = env.fs.load_json("context/meeting_context.json")
        assert ctx["summary"] == "Synthèse raccourcie."          # champ prioritaire du DOCX
        opts = env.fs.load_json("context/render_options.json")
        assert opts == {"sections": {"transcript": False}}       # filtré (junk éliminé)
        assert env.store.list_versions() == [1]                  # snapshot AVANT write-back
        assert rebuilt == ["j1"]                                 # package reconstruit
        assert "raccourcie" in env.store.load_turns()[-1]["text"]
        assert _FakeOpenCode.seen.get("review_path")             # points qualité fournis à l'agent

    def test_srt_guard_rejects_truncated_srt(self, env, monkeypatch):
        import transcria.exports.package_builder as pb
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: {})

        env.store.write_request(kind="apply", message="Reformule le segment 2")
        _FakeOpenCode.outputs = {
            "transcription_refined.srt": "1\n00:00:01,000 --> 00:00:02,000\nSPEAKER_00(Alice): Un seul segment.\n",
            "refine_report.md": "SRT reformulé.",
        }
        before = _srt_of(env)

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        assert _srt_of(env) == before                            # SRT original conservé
        assert "non conforme" in env.store.load_turns()[-1]["text"] or result.get("srt_updated") is False

    def test_apply_without_valid_output_leaves_all_untouched(self, env, monkeypatch):
        import transcria.exports.package_builder as pb
        called = []
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: called.append(1) or {})

        env.store.write_request(kind="apply", message="Fais quelque chose")
        _FakeOpenCode.outputs = {"structured_data_refined.json": "{pas du json"}
        before_ctx = env.fs.load_json("context/meeting_context.json")

        env.runner.run_refine(env.job, env.config)

        assert env.fs.load_json("context/meeting_context.json") == before_ctx
        assert env.store.list_versions() == []                   # pas de version fantôme
        assert called == []                                      # pas de rebuild inutile

    def test_apply_valid_srt_is_written_back(self, env, monkeypatch):
        import transcria.exports.package_builder as pb
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: {})

        refined = _SRT.replace("Segment numéro 2", "Segment DEUX reformulé")
        env.store.write_request(kind="apply", message="Reformule le segment 2")
        _FakeOpenCode.outputs = {"transcription_refined.srt": refined, "refine_report.md": "ok"}

        env.runner.run_refine(env.job, env.config)

        assert "Segment DEUX reformulé" in _srt_of(env)
        assert env.store.list_versions() == [1]


# ── Micro-étape « champs du type » (trou macro Word structuré) ────────────────

class TestRunTypeFieldExtraction:
    """Câblage bout-en-bout GPU-free : charge le transcript, appelle la LLM DIRECTE
    (chat_completion mocké), parse le JSON, fusionne dans structured_data, persiste."""

    def _set_custom_type(self, env, extract_fields):
        ctx = env.fs.load_json("context/meeting_context.json")
        ctx["custom_type"] = {"name": "Conseil E2E", "extract_fields": extract_fields}
        env.fs.save_json("context/meeting_context.json", ctx)

    def test_extrait_et_fusionne_les_champs_du_type(self, env):
        self._set_custom_type(env, [
            {"key": "deliberations", "label": "Délibérations", "instruction": "les délibérations"},
        ])
        _FakeChat.answer = '{"deliberations": ["Budget 2026 voté", "Travaux école"]}'

        result = env.runner.run_type_field_extraction(env.job, env.config)

        assert result["success"] is True
        assert result["fields_added"] == ["deliberations"]
        sd = env.fs.load_json("context/meeting_context.json")["structured_data"]
        assert sd["deliberations"] == ["Budget 2026 voté", "Travaux école"]
        assert sd["decisions"] == ["Décision A"]                 # existant préservé
        assert ("llm", "j1") in env.runner.allocator.released    # verrou LLM relâché
        # la transcription a bien été passée à la LLM
        user_msg = _FakeChat.seen["messages"][-1]["content"]
        assert "Segment numéro 1" in user_msg

    def test_sans_type_ne_touche_pas_a_la_llm(self, env):
        # pas de custom_type dans le contexte → court-circuit, aucun appel LLM
        result = env.runner.run_type_field_extraction(env.job, env.config)
        assert result["skipped"] is True and result["reason"] == "no_extract_fields"
        assert _FakeChat.seen == {}

    def test_reponse_llm_illisible_best_effort_rien_ajoute(self, env):
        self._set_custom_type(env, [
            {"key": "deliberations", "label": "Délibérations", "instruction": "les délibérations"},
        ])
        _FakeChat.answer = "je n'ai pas trouvé de JSON ici"

        result = env.runner.run_type_field_extraction(env.job, env.config)

        assert result["success"] is True and result["fields_added"] == []
        sd = env.fs.load_json("context/meeting_context.json").get("structured_data", {})
        assert "deliberations" not in sd                          # rien inventé


class TestSummaryStaleMarkerCleared:
    """§5.2 : la resynchronisation LLM (apply qui réécrit la synthèse) lève le
    marqueur « synthèse périmée » posé par l'éditeur SRT."""

    def test_apply_avec_synthese_leve_le_marqueur(self, env, monkeypatch):
        import transcria.exports.package_builder as pb
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: {})

        env.fs.save_json("metadata/summary_stale.json", {"since": "2026-07-18", "reason": "srt_edited"})
        env.store.write_request(kind="apply", message="Mets à jour la synthèse")
        _FakeOpenCode.outputs = {
            "summary_refined.md": "Synthèse resynchronisée.",
            "refine_report.md": "Synthèse mise à jour.",
        }

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        assert not (env.fs.job_dir / "metadata" / "summary_stale.json").exists()

    def test_apply_sans_synthese_garde_le_marqueur(self, env, monkeypatch):
        import transcria.exports.package_builder as pb
        monkeypatch.setattr(pb.PackageBuilder, "build_package", lambda self, job: {})

        env.fs.save_json("metadata/summary_stale.json", {"since": "2026-07-18"})
        env.store.write_request(kind="apply", message="Masque la transcription")
        _FakeOpenCode.outputs = {
            "render_options_refined.json": '{"sections": {"transcript": false}}',
            "refine_report.md": "Transcription masquée.",
        }

        env.runner.run_refine(env.job, env.config)

        assert (env.fs.job_dir / "metadata" / "summary_stale.json").exists()

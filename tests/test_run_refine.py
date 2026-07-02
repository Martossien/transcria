"""Phase ``refine`` (chat d'affinage des livrables) — GPU-free, opencode mocké.

Contrat (calqué sur ``run_final_review``) : best-effort intégral, verrou LLM +
réservation VRAM, ``AgentWorkspace`` isolé, garde-fous déterministes en sortie
(intégrité SRT, JSON valides, options de rendu filtrées), snapshot de version AVANT
tout write-back, conversation persistée dans ``refine/chat.json``.
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

    runner = WorkflowRunner(object, config)
    runner.allocator = _FakeAllocator()
    runner.vram = _FakeVram()
    runner.progress = _FakeProgress()

    import transcria.gpu.opencode_runner as ocr
    monkeypatch.setattr(ocr, "OpenCodeRunner", _FakeOpenCode)
    _FakeOpenCode.outputs, _FakeOpenCode.seen = {}, {}

    job = SimpleNamespace(id="j1", title="Réunion test")
    store = RefineStore(jobs_dir=str(jobs_dir), job_id="j1")
    return SimpleNamespace(runner=runner, config=config, fs=fs, job=job, store=store)


def _srt_of(env) -> str:
    return env.fs.load_text("metadata/transcription_corrigee.srt") or ""


# ── Mode discussion ───────────────────────────────────────────────────────────

class TestRunRefineDiscuss:
    def test_discuss_appends_answer_no_file_change(self, env):
        env.store.write_request(kind="discuss", message="De quoi parle la réunion ?")
        _FakeOpenCode.outputs = {"refine_answer.md": "La réunion porte sur…\n---\nProposition : rien."}
        before = _srt_of(env)

        result = env.runner.run_refine(env.job, env.config)

        assert result["success"] is True
        turns = env.store.load_turns()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert "porte sur" in turns[1]["text"]
        assert _srt_of(env) == before                      # aucun artefact modifié
        assert env.store.has_active_request() is False     # demande consommée

    def test_discuss_proposal_extracted_into_turn(self, env):
        env.store.write_request(kind="discuss", message="Peut-on raccourcir ?")
        _FakeOpenCode.outputs = {"refine_answer.md": (
            "Oui, la synthèse peut être condensée.\n\n---\n"
            "Proposition d'application : raccourcir la synthèse de moitié."
        )}

        env.runner.run_refine(env.job, env.config)

        turn = env.store.load_turns()[-1]
        assert turn["proposal"] == "raccourcir la synthèse de moitié."
        assert "Proposition" not in turn["text"]     # bloc retiré du texte affiché

    def test_conversation_context_is_fed_to_agent(self, env):
        # Un tour précédent existe → le fichier conversation transmis à l'agent le contient.
        env.store.append_turn(role="user", kind="discuss", text="Premier échange ?")
        env.store.append_turn(role="assistant", kind="discuss", text="Réponse initiale mémorable.")
        env.store.write_request(kind="discuss", message="Et ensuite ?")
        _FakeOpenCode.outputs = {"refine_answer.md": "Suite."}

        env.runner.run_refine(env.job, env.config)

        assert "mémorable" in _FakeOpenCode.seen.get("conversation_text", "")

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

    def test_opencode_failure_gives_feedback_turn(self, env):
        env.store.write_request(kind="discuss", message="?")

        class _Boom(_FakeOpenCode):
            def run_refine(self, **kwargs):
                raise RuntimeError("opencode indisponible")

        import transcria.gpu.opencode_runner as ocr
        ocr.OpenCodeRunner = _Boom
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

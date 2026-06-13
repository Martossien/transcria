"""Isolation des agents LLM (AgentWorkspace) — voir docs/PIPELINE_REPRISE.md.

Incident fondateur (job 4bda98cb) : l'agent de correction (cwd=metadata/, Edit actif) a
réécrit transcription.srt, l'artefact SOURCE. Contrat testé ici : scratch + copies,
sources canoniques immuables (restaurées si mutées), scratch jamais synchronisé.

Régression d'infra (job 6f4f4cad, 13/06/2026) : le scratch sous l'arbre du dépôt faisait
charger AGENTS.md (95 Ko) par opencode et ancrait ses outils sur la racine git → l'agent
de relecture déraillait (chemins relatifs cassés, évasion /tmp bloquée en headless). Le
scratch vit désormais HORS du dépôt (`storage.agent_work_dir`, défaut tempdir système).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from transcria.jobs.filesystem import JobFilesystem
from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root


def _fs_and_root(tmp_path):
    """Filesystem de job + un work_root HORS de l'arbre des jobs (comme en production)."""
    fs = JobFilesystem(str(tmp_path / "jobs"), "job-ws")
    fs.save_text("metadata/transcription.srt", "1\n00:00:00,000 --> 00:00:01,000\nbrut\n")
    fs.save_text("context/job_context.yaml", "titre: réunion\n")
    return fs, str(tmp_path / "agent_work")


class TestScratchLifecycle:
    def test_stage_copies_into_scratch(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        staged = ws.stage("metadata/transcription.srt")
        assert staged.parent == ws.scratch_dir
        assert staged.read_text(encoding="utf-8") == (fs.job_dir / "metadata/transcription.srt").read_text(encoding="utf-8")
        # Le scratch vit sous <work_root>/<job_id>/<phase>, HORS de job_dir (et du dépôt).
        assert ws.scratch_dir == Path(work_root) / "job-ws" / "correction"
        assert fs.job_dir not in ws.scratch_dir.parents

    def test_stage_missing_optional_input_returns_scratch_path(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        staged = ws.stage("context/session_lexicon_filtered.json")
        assert not staged.exists()  # entrée optionnelle absente : chemin cohérent, pas de fichier
        assert staged.parent == ws.scratch_dir

    def test_stage_name_collision_raises(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        fs.save_text("summary/job_context.yaml", "autre")
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        ws.stage("context/job_context.yaml")
        with pytest.raises(ValueError):
            ws.stage("summary/job_context.yaml")

    def test_scratch_purged_on_entry(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        leftover = Path(work_root) / "job-ws" / "correction" / "reste_du_run_precedent.txt"
        leftover.parent.mkdir(parents=True)
        leftover.write_text("debris")
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        assert not leftover.exists()
        assert ws.scratch_dir.is_dir()

    def test_cleanup_removes_scratch_on_success_keeps_on_failure(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        (ws.scratch_dir / "out.md").write_text("sortie")
        ws.cleanup(success=False)
        assert ws.scratch_dir.is_dir()  # conservé pour diagnostic
        ws2 = AgentWorkspace(fs, "correction", work_root=work_root)
        ws2.cleanup(success=True)
        assert not ws2.scratch_dir.exists()

    def test_read_output(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        (ws.scratch_dir / "transcription_corrigee.srt").write_text("corrigé\n", encoding="utf-8")
        assert ws.read_output("transcription_corrigee.srt") == "corrigé"
        assert ws.read_output("inexistant.md") == ""

    def test_legacy_fallback_without_work_root_uses_job_dir(self, tmp_path):
        """Sans work_root (chemin de repli), le scratch reste sous job_dir/work/<phase>."""
        fs, _ = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction")
        assert ws.scratch_dir == fs.job_dir / "work" / "correction"


class TestScratchOutsideRepo:
    """Cause racine de l'incident 6f4f4cad : aucun fichier de règles ne doit être un
    ancêtre du scratch, sinon opencode l'injecte dans le contexte de l'agent."""

    def test_scratch_has_no_rules_file_ancestor(self, tmp_path):
        repo = tmp_path / "repo"
        (repo).mkdir()
        (repo / "AGENTS.md").write_text("# 95 Ko de doc dev\n")  # simule le dépôt
        fs = JobFilesystem(str(repo / "jobs"), "job-ws")
        fs.save_text("metadata/transcription.srt", "x")
        work_root = tmp_path / "agent_work"  # HORS du dépôt

        ws = AgentWorkspace(fs, "final_review", work_root=str(work_root))

        offenders = [
            str(anc / marker)
            for anc in [ws.scratch_dir, *ws.scratch_dir.parents]
            for marker in ("AGENTS.md", "CLAUDE.md")
            if (anc / marker).exists()
        ]
        assert offenders == [], f"fichier de règles dans un ancêtre du scratch : {offenders}"
        assert repo not in ws.scratch_dir.parents


class TestResolveAgentWorkRoot:
    def test_default_is_system_tempdir(self):
        root = resolve_agent_work_root({})
        assert root.endswith("transcria-agent-work")
        assert root.startswith(tempfile.gettempdir())

    def test_none_config_default(self):
        assert resolve_agent_work_root(None).endswith("transcria-agent-work")

    def test_override_honoured(self):
        assert resolve_agent_work_root({"storage": {"agent_work_dir": "/data/scratch"}}) == "/data/scratch"

    def test_blank_override_falls_back(self):
        assert resolve_agent_work_root({"storage": {"agent_work_dir": "   "}}).endswith("transcria-agent-work")


class TestPurgeJob:
    def test_removes_job_scratch_tree(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "final_review", work_root=work_root)
        (ws.scratch_dir / "out.md").write_text("x")
        ws.cleanup(success=False)  # échec → scratch conservé
        assert ws.scratch_dir.is_dir()

        AgentWorkspace.purge_job(work_root, "job-ws")
        assert not (Path(work_root) / "job-ws").exists()

    def test_noop_on_blank_inputs(self, tmp_path):
        AgentWorkspace.purge_job(None, "job-ws")  # ne lève pas
        AgentWorkspace.purge_job(str(tmp_path), "")  # ne lève pas


class TestSourceImmutabilityGuard:
    def test_mutated_staged_source_is_restored(self, tmp_path):
        """LE scénario de l'incident : l'agent réécrit le SRT source canonique."""
        fs, work_root = _fs_and_root(tmp_path)
        pristine = (fs.job_dir / "metadata/transcription.srt").read_text(encoding="utf-8")
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        ws.stage("metadata/transcription.srt")

        # L'agent déborde du scratch (chemin absolu) et corrompt la source.
        (fs.job_dir / "metadata/transcription.srt").write_text("CORROMPU PAR L'AGENT", encoding="utf-8")

        violations = ws.verify_and_restore_sources()
        assert "metadata/transcription.srt" in violations
        assert (fs.job_dir / "metadata/transcription.srt").read_text(encoding="utf-8") == pristine

    def test_agent_created_canonical_file_is_removed(self, tmp_path):
        """Entrée optionnelle absente : si l'agent CRÉE le fichier canonique, on revient
        à l'état pristine (absent)."""
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        ws.stage("context/session_lexicon_filtered.json")

        target = fs.job_dir / "context" / "session_lexicon_filtered.json"
        target.write_text("[]", encoding="utf-8")

        violations = ws.verify_and_restore_sources()
        assert "context/session_lexicon_filtered.json" in violations
        assert not target.exists()

    def test_unstaged_watched_mutation_is_detected(self, tmp_path):
        """Fichier canonique non stagé altéré pendant le run : signalé (pas de copie
        pristine pour restaurer — en pg, un re-pull répare)."""
        fs, work_root = _fs_and_root(tmp_path)
        fs.save_text("metadata/transcription_segments.json", "[1]")
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        ws.stage("metadata/transcription.srt")

        fs.save_text("metadata/transcription_segments.json", "[2]")  # mutation hors stage

        violations = ws.verify_and_restore_sources()
        assert "metadata/transcription_segments.json" in violations
        # Pas de restauration possible : le contenu muté reste, mais c'est VISIBLE.
        assert (fs.job_dir / "metadata/transcription_segments.json").read_text(encoding="utf-8") == "[2]"

    def test_clean_run_reports_no_violation(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "correction", work_root=work_root)
        ws.stage("metadata/transcription.srt")
        (ws.scratch_dir / "transcription_corrigee.srt").write_text("corrigé", encoding="utf-8")
        assert ws.verify_and_restore_sources() == []

    def test_scratch_writes_are_not_violations(self, tmp_path):
        fs, work_root = _fs_and_root(tmp_path)
        ws = AgentWorkspace(fs, "final_review", work_root=work_root)
        ws.write_input("final_review_glossary.md", "glossaire")
        (ws.scratch_dir / "summary_harmonized.md").write_text("ok", encoding="utf-8")
        assert ws.verify_and_restore_sources() == []


class TestSyncInvariant:
    def test_work_dir_is_outside_sync_whitelist(self):
        """`work/` ne doit JAMAIS rejoindre la whitelist de synchro : un scratch d'agent
        n'est pas un artefact canonique et ne doit pas atterrir en base."""
        from transcria.jobs import artifact_store
        assert not any(p.startswith("work") for p in artifact_store.SYNCED_PREFIXES)
        assert not any(p.startswith("work") for p in artifact_store.INPUT_PREFIXES)
        assert not any(p.startswith("work") for p in artifact_store.WEB_WRITE_PREFIXES)

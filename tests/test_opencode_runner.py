"""Tests for OpenCodeRunner — run(), _parse_structured_summary, run_correction, error paths."""
import json
import os
import subprocess
import time

import pytest

from transcria.gpu.opencode_runner import OpenCodeRunner, _get_prompts_dir


def _make_runner(tmp_path, **kwargs):
    kwargs.setdefault("model", "local/test-llm-arbitrage")
    return OpenCodeRunner(str(tmp_path), **kwargs)


class _FakePopen:
    """Simule subprocess.Popen pour les tests de OpenCodeRunner (interface streaming).

    ``running_polls`` : nombre d'appels poll() renvoyant None (process « en cours »)
    avant de renvoyer le returncode — pour simuler un GEL (process qui ne sort pas)."""
    def __init__(self, stdout="", stderr="", returncode=0, communicate_exc=None,
                 running_polls=0):
        import io
        self.pid = 99999
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self._communicate_exc = communicate_exc
        self._running_polls = running_polls
        self._terminated = False

    def poll(self):
        if self._communicate_exc is not None and not self._terminated:
            # simule un process qui reste vivant jusqu'au kill du watchdog
            return None
        if self._running_polls > 0:
            self._running_polls -= 1
            return None
        return self.returncode

    def communicate(self, timeout=None):  # conservé pour compat (non utilisé par run())
        if self._communicate_exc is not None:
            raise self._communicate_exc
        return self.stdout.getvalue(), self.stderr.getvalue()

    def send_signal(self, sig):
        self._terminated = True

    def wait(self, timeout=None):
        pass


def _fake_popen(stdout="", stderr="", returncode=0, communicate_exc=None, popen_exc=None):
    """Retourne une factory Popen simulée."""
    def factory(cmd, **kw):
        if popen_exc is not None:
            raise popen_exc
        return _FakePopen(stdout, stderr, returncode, communicate_exc)
    return factory


class TestOpenCodeRunnerInit:
    def test_explicit_model_ref_format(self, tmp_path):
        runner = _make_runner(tmp_path, model="local/test-llm-arbitrage")
        assert runner.model == "test-llm-arbitrage"
        assert runner.provider == "local"
        assert runner.model_ref == "local/test-llm-arbitrage"

    def test_model_id_from_config(self, tmp_path):
        runner = OpenCodeRunner(
            str(tmp_path),
            config={"workflow": {"arbitration_llm": {"model_id": "remote/test-llm"}}},
        )
        assert runner.model == "test-llm"
        assert runner.provider == "remote"
        assert runner.model_ref == "remote/test-llm"

    def test_no_model_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="model_id"):
            OpenCodeRunner(str(tmp_path))

    def test_no_model_in_config_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="model_id"):
            OpenCodeRunner(str(tmp_path), config={"workflow": {"arbitration_llm": {"model_id": ""}}})

    def test_custom_model_and_provider(self, tmp_path):
        runner = _make_runner(tmp_path, model="custom-model", provider="remote")
        assert runner.model_ref == "remote/custom-model"

    def test_custom_opencode_bin(self, tmp_path):
        runner = _make_runner(tmp_path, opencode_bin="/usr/local/bin/opencode")
        assert runner.opencode_bin == "/usr/local/bin/opencode"

    def test_work_dir_resolved(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.work_dir == tmp_path.resolve()

    def test_correction_timeout_comes_from_config(self, tmp_path):
        runner = _make_runner(
            tmp_path,
            config={"workflow": {"arbitration_llm": {"timeout_seconds": 1234}}},
        )
        assert runner._get_correction_timeout() == 1234

    def test_summary_timeout_comes_from_config(self, tmp_path):
        runner = _make_runner(
            tmp_path,
            config={"workflow": {"summary_llm": {"timeout_seconds": 4321}}},
        )
        assert runner._get_summary_timeout() == 4321


class TestOpenCodeRunnerRun:
    def test_run_opencode_not_found(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        runner = _make_runner(tmp_path)
        result = runner.run("test instruction", "/tmp/prompt.txt")
        assert result["success"] is False
        assert "introuvable" in result["error"]

    def test_run_prompt_file_not_found(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        runner = _make_runner(tmp_path)
        result = runner.run("test instruction", "/nonexistent/prompt.txt")
        assert result["success"] is False
        assert "Prompt introuvable" in result["error"]

    def _fast_watchdog_cfg(self):
        # intervalles courts pour un watchdog testable en < 1 s
        return {"workflow": {"arbitration_llm": {
            "opencode_watchdog_poll_s": 0.02,
            "opencode_idle_grace_s": 0.05,
            "opencode_pure_idle_cap_s": 0.2,
        }}}

    def _run_hung(self, tmp_path, monkeypatch, llm_processing):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        # process GELÉ : poll() reste None (communicate_exc), aucune sortie
        monkeypatch.setattr(subprocess, "Popen",
            _fake_popen(communicate_exc=subprocess.TimeoutExpired(cmd=[], timeout=600)))
        runner = _make_runner(tmp_path, config=self._fast_watchdog_cfg())
        monkeypatch.setattr(runner, "_llm_is_processing", lambda: llm_processing)
        return runner.run("test instruction", "/tmp/prompt.txt", timeout=600)

    def test_watchdog_tue_le_gel_silencieux_llm_idle(self, tmp_path, monkeypatch):
        # opencode silencieux + LLM idle → gel détecté (opencode#17516) → tué vite
        result = self._run_hung(tmp_path, monkeypatch, llm_processing=False)
        assert result["success"] is False
        assert "interrompu" in result["error"].lower() or "gel" in result["error"].lower()

    def test_watchdog_repli_idle_pur_quand_etat_llm_inconnu(self, tmp_path, monkeypatch):
        # /slots indisponible (None) : le repli idle pur (0.2 s) tue quand même le gel
        result = self._run_hung(tmp_path, monkeypatch, llm_processing=None)
        assert result["success"] is False
        assert "idle pur" in result["error"].lower() or "interrompu" in result["error"].lower()

    def test_run_generic_exception(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(subprocess, "Popen",
            _fake_popen(popen_exc=OSError("fork failed")))

        runner = _make_runner(tmp_path)
        result = runner.run("test instruction", "/tmp/prompt.txt")
        assert result["success"] is False
        assert "Échec" in result["error"]

    def test_run_nonzero_exit_code(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(subprocess, "Popen",
            _fake_popen(stdout="", stderr="Error: model not found", returncode=1))

        runner = _make_runner(tmp_path)
        result = runner.run("test instruction", "/tmp/prompt.txt")
        assert result["success"] is False
        assert "exit 1" in result["error"]
        assert "model not found" in result["error"]

    def test_run_success_with_events(self, tmp_path, monkeypatch):
        import shutil
        events = [
            {"type": "text", "part": {"text": "Bonjour ceci est le résumé."}},
            {"type": "tool_use", "part": {"tool": "write_file", "input": {"path": "summary.md"}}},
            {"type": "text", "part": {"text": " Fin du résumé."}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(time, "time", lambda: 1000000)
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(stdout=stdout, returncode=0))

        runner = _make_runner(tmp_path)
        result = runner.run("Fais un résumé", "/tmp/prompt.txt")
        assert result["success"] is True
        assert result["events_count"] == 3
        assert result["tool_calls"] == 1
        assert "Bonjour ceci est le résumé." in result["output"]
        assert "Fin du résumé." in result["output"]

    def test_run_success_malformed_json_lines_skipped(self, tmp_path, monkeypatch):
        import shutil
        stdout_lines = [
            '{"type": "text", "part": {"text": "OK"}}',
            'not json at all',
            '{"type": "text", "part": {"text": "Suite"}}',
            '',
        ]
        stdout = "\n".join(stdout_lines)

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(time, "time", lambda: 1000000)
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(stdout=stdout, returncode=0))

        runner = _make_runner(tmp_path)
        result = runner.run("Test", "/tmp/prompt.txt")
        assert result["success"] is True
        assert result["events_count"] == 2
        assert "OK" in result["output"]
        assert "Suite" in result["output"]

    def test_run_success_no_events(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(time, "time", lambda: 1000000)
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(stdout="", returncode=0))

        runner = _make_runner(tmp_path)
        result = runner.run("Test", "/tmp/prompt.txt")
        assert result["success"] is True
        assert result["output"] == ""
        assert result["events_count"] == 0
        assert result["tool_calls"] == 0

    def test_run_with_absolute_path_opencode_bin(self, tmp_path, monkeypatch):
        import shutil
        opencode_bin = str(tmp_path / "custom_opencode")
        captured_cmd = {}

        def capturing_popen(cmd, **kw):
            captured_cmd["cmd"] = cmd
            return _FakePopen(stdout="{}", returncode=0)

        monkeypatch.setattr(shutil, "which", lambda name: None)
        (tmp_path / "custom_opencode").write_text("#!/bin/bash\n")
        monkeypatch.setattr(os.path, "isfile", lambda p: p == str(tmp_path / "custom_opencode") or p.endswith(".txt"))
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(subprocess, "Popen", capturing_popen)

        runner = _make_runner(tmp_path, opencode_bin=opencode_bin)
        result = runner.run("Test", "/tmp/prompt.txt")
        assert result["success"] is True
        assert opencode_bin in captured_cmd["cmd"][0] or "custom_opencode" in captured_cmd["cmd"][0]

    def test_run_sets_tmpdir_to_work_dir(self, tmp_path, monkeypatch):
        """TMPDIR = scratch : les fichiers temporaires réflexes restent in-project
        (sinon /tmp = external_directory rejeté en headless → run avorté en silence)."""
        import shutil
        captured = {}

        def capturing_popen(cmd, **kw):
            captured["cmd"] = cmd
            captured.update(kw)
            return _FakePopen(stdout="{}", returncode=0)

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(subprocess, "Popen", capturing_popen)

        runner = _make_runner(tmp_path)
        runner.run("Test", str(tmp_path / "prompt.txt"))
        assert captured["env"]["TMPDIR"] == str(runner.work_dir)
        assert "PATH" in captured["env"]  # l'env parent est préservé, pas remplacé
        # --dir fixe la racine de projet opencode sur le scratch (sinon opencode reste
        # ancré sur PWD/dépôt → AGENTS.md chargé + entrées stagées « external »).
        assert "--dir" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--dir") + 1] == str(runner.work_dir)

    def test_run_creates_work_dir(self, tmp_path, monkeypatch):
        import shutil
        work_dir = tmp_path / "new_subdir" / "workspace"
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(time, "time", lambda: 1000000)
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(stdout="{}", returncode=0))

        runner = OpenCodeRunner(str(work_dir), model="local/test-llm-arbitrage")
        result = runner.run("Test", "/tmp/prompt.txt")
        assert work_dir.is_dir()


class TestOpenCodeRunnerParseStructuredSummary:
    def test_parse_full_summary(self):
        text = """# Résumé de contrôle

**Titre suggéré :** Réunion Budget Q1 2026

**Type suggéré :** Réunion interne

**Sujet principal :** Révision du budget trimestriel

**Objectif probable :** Valider les enveloppes budgétaires

**Notes / Ordre du jour probable :** Tour de table, présentation des écarts

**Nombre de participants détectés :** 4

## Participants probables

- Alice Martin (DSI)
- Bob Dupont (DCF)
- Claire Lefèvre (DRH)
- Non identifiable (SPEAKER_03)

**Mots-clés**

budget, Q1, écarts, enveloppes, validation

## Termes suspects

- **API Gateway** [technique](critique)
- **Kubernetes** [technique](normale)
- **Non identifiable terme**

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["title_suggere"] == "Réunion Budget Q1 2026"
        assert result["type_suggere"] == "Réunion interne"
        assert result["sujet_suggere"] == "Révision du budget trimestriel"
        assert result["objectif_suggere"] == "Valider les enveloppes budgétaires"
        assert result["notes_suggeres"] == "Tour de table, présentation des écarts"
        assert result["speaker_count"] == 4
        assert "Alice Martin" in result["participants_detectes"]
        assert "Bob Dupont" in result["participants_detectes"]
        assert "Non identifiable" not in result["participants_detectes"]
        assert "budget" in result["mots_cles"]
        assert len(result["termes_suspects"]) >= 2
        assert result["termes_suspects"][0]["term"] == "API Gateway"
        assert result["termes_suspects"][0]["category"] == "technique"
        assert result["termes_suspects"][0]["priority"] == "critique"
        # Provenance par défaut : non-document (recoupement documents non signalé).
        assert result["termes_suspects"][0].get("source", "") == ""

    def test_parse_term_source_document_inline(self):
        text = (
            "## Termes douteux à valider\n\n"
            "- **Kubernetes** [technique] (importante) | variantes_suspectes: cubernétisse "
            "| commentaire: forme du document présenté | source: document | "
            'contextes: [00:03:10] SPEAKER_01: "on déploie sur cubernétisse"\n'
            "- **Terme normal** [métier] (normale) | variantes_suspectes: variante A "
            '| commentaire: faute STT | contextes: [00:04:00] SPEAKER_02: "variante A"\n'
        )
        result = OpenCodeRunner._parse_structured_summary(text)
        terms = {t["term"]: t for t in result["termes_suspects"]}
        assert terms["Kubernetes"]["source"] == "document"
        assert "cubernétisse" in terms["Kubernetes"]["variants"]
        # Une entrée ordinaire ne reçoit jamais la provenance document.
        assert terms["Terme normal"].get("source", "") == ""

    def test_parse_term_source_document_table(self):
        text = (
            "## Termes douteux à valider\n\n"
            "| terme | variantes | catégorie | priorité | source |\n"
            "|---|---|---|---|---|\n"
            "| Kubernetes | cubernétisse | technique | importante | document |\n"
        )
        result = OpenCodeRunner._parse_structured_summary(text)
        terms = {t["term"]: t for t in result["termes_suspects"]}
        assert terms["Kubernetes"]["source"] == "document"

    def test_parse_empty_text(self):
        result = OpenCodeRunner._parse_structured_summary("")
        assert result["title_suggere"] == ""
        assert result["speaker_count"] == 0
        assert result["termes_suspects"] == []

    def test_parse_partial_fields(self):
        text = """**Titre suggéré :** Point hebdomadaire

Quelques paragraphes de texte sans autres champs structurés.
"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["title_suggere"] == "Point hebdomadaire"
        assert result["type_suggere"] == ""
        assert result["speaker_count"] == 0

    def test_parse_speaker_count_extraction(self):
        text = "**Nombre de participants détectés :** 7\n"
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["speaker_count"] == 7

    def test_parse_participants_excludes_non_identifiable(self):
        text = """## Participants probables

- Jean Martin (DPF)
- Non identifiable (SPEAKER_02)
- Sophie Durand (DCG)
"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert "Jean Martin" in result["participants_detectes"]
        assert "Sophie Durand" in result["participants_detectes"]
        assert "Non identifiable" not in result["participants_detectes"]

    def test_parse_speaker_roles_splits_label_dash_role(self):
        text = """## Participants probables

- SPEAKER_01 : Fonction B — décrit une action observée
- SPEAKER_00 : Fonction A — décrit une autre action observée
"""
        result = OpenCodeRunner._parse_structured_summary(text)

        assert result["speaker_roles"]["SPEAKER_00"] == {
            "label": "Fonction A",
            "role": "décrit une autre action observée",
        }
        assert result["speaker_roles"]["SPEAKER_01"] == {
            "label": "Fonction B",
            "role": "décrit une action observée",
        }

    def test_parse_speaker_roles_bracket_format(self):
        text = """## Participants probables

- SPEAKER_00 [Fonction A] : décrit une action observée
"""
        result = OpenCodeRunner._parse_structured_summary(text)

        assert result["speaker_roles"]["SPEAKER_00"] == {
            "label": "Fonction A",
            "role": "décrit une action observée",
        }

    def test_parse_speaker_roles_keeps_non_identifiable_inside_role(self):
        text = """## Participants probables

- SPEAKER_00 [Alex Dupont] : personne s'identifiant dans un extrait vocal (rôle non identifiable au-delà de l'auto-désignation)
"""
        result = OpenCodeRunner._parse_structured_summary(text)

        assert "Alex Dupont" in result["participants_detectes"]
        assert result["speaker_roles"]["SPEAKER_00"] == {
            "label": "Alex Dupont",
            "role": "personne s'identifiant dans un extrait vocal (rôle non identifiable au-delà de l'auto-désignation)",
        }

    def test_parse_speaker_roles_excludes_non_identifiable_speaker_placeholder(self):
        text = """## Participants probables

- SPEAKER_00 [Non identifiable] : non identifiable
- SPEAKER_01 [Fonction A] : décrit une action observée
"""
        result = OpenCodeRunner._parse_structured_summary(text)

        assert "SPEAKER_00" not in result["participants_detectes"]
        assert "SPEAKER_00" not in result["speaker_roles"]
        assert result["speaker_roles"]["SPEAKER_01"]["label"] == "Fonction A"

    def test_parse_mots_cles_multiline(self):
        text = """**Mots-clés**

budget, écarts, prévision
validation, synthèse

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert "budget" in result["mots_cles"]
        assert "validation" in result["mots_cles"]

    def test_parse_termes_suspects_simple_format(self):
        text = """## Termes suspects

- **Kubernetes** [technique](critique)
- SimpleTerme [](normale)

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        terms = result["termes_suspects"]
        assert any(t["term"] == "Kubernetes" for t in terms)
        assert len(terms) >= 1

    def test_parse_termes_suspects_with_variants_and_comment(self):
        text = """## Termes douteux à valider

- **SIGLE_REF** [sigle / métier] (critique) | variantes_suspectes: SIGLE_ERR | commentaire: une variante semble une erreur STT à valider. | contextes: [00:12:34] SPEAKER_01: "extrait contenant SIGLE_ERR"

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        term = result["termes_suspects"][0]
        assert term["term"] == "SIGLE_REF"
        assert term["category"] == "sigle / métier"
        assert term["priority"] == "critique"
        assert term["variants"] == ["SIGLE_ERR"]
        assert "variante semble" in term["comment"]
        assert term["contexts"] == [{
            "variant": "",
            "timecode": "00:12:34",
            "speaker": "SPEAKER_01",
            "quote": "extrait contenant SIGLE_ERR",
            "reason": "",
        }]

    def test_parse_termes_suspects_markdown_table(self):
        text = """## Termes douteux à valider

| Terme | Catégorie | Priorité | Variantes suspectes | Commentaire | Contextes |
|---|---|---|---|---|---|
| Émental | métier | critique | émenteal ; émental | orthographe à valider | [00:00:05] SPEAKER_00: "De l'émenteal" |

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        term = result["termes_suspects"][0]
        assert result["termes_suspects_parse_status"] == "extracted"
        assert term["term"] == "Émental"
        assert term["category"] == "métier"
        assert term["priority"] == "critique"
        assert term["variants"] == ["émenteal"]
        assert term["comment"] == "orthographe à valider"
        assert term["contexts"][0]["timecode"] == "00:00:05"
        assert term["contexts"][0]["speaker"] == "SPEAKER_00"

    def test_parse_termes_suspects_loose_pipe_fields(self):
        text = """## Termes douteux à valider

- Terme validé | catégorie: métier | priorité: importante | variantes: Variante A, variante B | justification: forme sensible à confirmer

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        term = result["termes_suspects"][0]
        assert term["term"] == "Terme validé"
        assert term["category"] == "métier"
        assert term["priority"] == "importante"
        assert term["variants"] == ["Variante A", "variante B"]
        assert term["comment"] == "forme sensible à confirmer"

    def test_parse_termes_suspects_empty_section_status(self):
        text = """## Termes douteux à valider

(aucun terme suspect détecté)

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["termes_suspects"] == []
        assert result["termes_suspects_parse_status"] == "empty"
        assert result["termes_suspects_parse_warning"] == ""

    def test_parse_termes_suspects_unparsed_section_status(self):
        text = """## Termes douteux à valider

La transcription contient peut-être des termes métier, mais je ne peux pas les isoler.

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["termes_suspects"] == []
        assert result["termes_suspects_parse_status"] == "section_unparsed"
        assert "aucun terme extrait" in result["termes_suspects_parse_warning"]

    def test_parse_termes_suspects_with_multiple_contexts(self):
        text = """## Termes douteux à valider

- **Terme métier A** [métier] (critique) | variantes_suspectes: Variante A ; Variante B | commentaire: deux formes douteuses. | contextes: [00:01:02] SPEAKER_00: "extrait Variante A" || [00:03:04] SPEAKER_01: "extrait Variante B" (contexte clair)

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        contexts = result["termes_suspects"][0]["contexts"]
        assert len(contexts) == 2
        assert contexts[0]["quote"] == "extrait Variante A"
        assert contexts[1]["reason"] == "contexte clair"

    def test_parse_termes_suspects_context_without_opening_bracket_and_placeholder_speaker(self):
        text = """## Termes douteux à valider

- **Emmental** [produit] (critique) | variantes_suspectes: émenteal | commentaire: à vérifier. | contextes: 00:05] SPEAKER_XX: « De l'émenteal, ça ira comme ça ? »

"""
        result = OpenCodeRunner._parse_structured_summary(text)

        context = result["termes_suspects"][0]["contexts"][0]
        assert context["timecode"] == "00:05"
        assert context["speaker"] == "SPEAKER_XX"
        assert context["quote"] == "De l'émenteal, ça ira comme ça ?"

    def test_parse_termes_suspects_context_strips_llm_quote_wrappers(self):
        context = '"[00:05] SPEAKER_XX: "De l\'émenteal, ça ira comme ça ?""'
        text = """## Termes douteux à valider

- **Emmental** [produit] (critique) | variantes_suspectes: émenteal | commentaire: à vérifier. | contextes: {context}

""".format(context=context)
        result = OpenCodeRunner._parse_structured_summary(text)

        context = result["termes_suspects"][0]["contexts"][0]
        assert context["timecode"] == "00:05"
        assert context["speaker"] == "SPEAKER_XX"
        assert context["quote"] == "De l'émenteal, ça ira comme ça ?"

    def test_parse_termes_suspects_context_long_meeting_mmss_over_99(self):
        text = """## Termes douteux à valider

- **C2S** [sigle] (critique) | variantes_suspectes: C25 | commentaire: sigle. | contextes: [89:05] SPEAKER_06: "actualités C2S" || [118:00] SPEAKER_01: "partie C2S" || [185:30] SPEAKER_06: "reprise C2S"

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        contexts = result["termes_suspects"][0]["contexts"]
        assert len(contexts) == 3
        assert contexts[0]["timecode"] == "89:05"
        assert contexts[0]["quote"] == "actualités C2S"
        assert contexts[1]["timecode"] == "118:00"
        assert contexts[1]["quote"] == "partie C2S"
        assert contexts[2]["timecode"] == "185:30"
        assert contexts[2]["quote"] == "reprise C2S"

    def test_parse_termes_suspects_context_hhmmss_three_digit_hours(self):
        text = """## Termes douteux à valider

- **SIGLE_A** [sigle] (critique) | variantes_suspectes: SIGLE_B | commentaire: test. | contextes: [03:05:30] SPEAKER_01: "mention SIGLE_A"

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        context = result["termes_suspects"][0]["contexts"][0]
        assert context["timecode"] == "03:05:30"
        assert context["quote"] == "mention SIGLE_A"

    def test_parse_termes_suspects_normalizes_empty_and_duplicate_variants(self):
        text = """## Termes douteux à valider

- **Forme validée** [organisation] (normale) | variantes_suspectes: (aucune) ; forme validée ; Graphie suspecte ; graphie suspecte | commentaire: forme sensible à valider.

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        term = result["termes_suspects"][0]
        assert term["term"] == "Forme validée"
        assert term["variants"] == ["Graphie suspecte"]

    def test_parse_termes_suspects_legacy_heading(self):
        text = """## Termes suspects

- **Terme métier A** [métier] (importante)

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        assert result["termes_suspects"][0]["term"] == "Terme métier A"

    def test_parse_termes_suspects_legacy_slash_as_variants(self):
        text = """## Termes suspects

- **Terme métier A / Variante phonétique A** [métier / spécialité] (critique) : variantes STT probables

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        term = result["termes_suspects"][0]
        assert term["term"] == "Terme métier A"
        assert term["variants"] == ["Variante phonétique A"]
        assert term["comment"] == "variantes STT probables"

    def test_parse_termes_suspects_excludes_non_identifiable(self):
        text = """## Termes suspects

- **API** [technique](critique)
- Non identifiable terme

"""
        result = OpenCodeRunner._parse_structured_summary(text)
        terms = result["termes_suspects"]
        terms_names = [t["term"] for t in terms]
        assert "API" in terms_names
        assert all("Non identifiable" not in t for t in terms_names)


class TestOpenCodeRunnerRunSummary:
    def test_run_summary_uses_configured_timeout(self, tmp_path, monkeypatch):
        (tmp_path / "quick_transcript.txt").write_text("Bonjour", encoding="utf-8")
        prompt_dir = os.path.join(_get_prompts_dir())
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, "summary_prompt.txt")
        if not os.path.isfile(prompt_file):
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write("Tu es un assistant.")

        captured = {}

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            captured["timeout"] = timeout
            return {"success": True, "output": "Résumé généré", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(
            tmp_path,
            config={"workflow": {"summary_llm": {"timeout_seconds": 4321}}},
        )
        result = runner.run_summary(str(tmp_path / "quick_transcript.txt"))
        assert result["summary_text"] == "Résumé généré"
        assert captured["timeout"] == 4321

    def test_run_summary_reads_summary_md(self, tmp_path, monkeypatch):
        # Contrat : run_summary n'utilise summary.md QUE si opencode l'a (ré)écrit pendant
        # le run (mtime postérieur). On simule donc opencode qui écrit le résumé structuré.
        (tmp_path / "quick_transcript.txt").write_text("[0s->1s] Bonjour", encoding="utf-8")
        prompt_dir = os.path.join(_get_prompts_dir())
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, "summary_prompt.txt")
        if not os.path.isfile(prompt_file):
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write("Tu es un assistant.")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            (tmp_path / "summary.md").write_text("# Résumé\n\n**Titre suggéré :** Mon titre\n", encoding="utf-8")
            return {"success": True, "output": "", "files": [str(tmp_path / "summary.md")], "events_count": 2, "tool_calls": 1}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path)
        result = runner.run_summary(str(tmp_path / "quick_transcript.txt"))
        assert result["_summary_produced"] is True
        assert result["title_suggere"] == "Mon titre"
        assert "Mon titre" in result["summary_text"]

    def test_run_summary_fallback_to_output_when_no_md(self, tmp_path, monkeypatch):
        (tmp_path / "quick_transcript.txt").write_text("Bonjour", encoding="utf-8")
        prompt_dir = os.path.join(_get_prompts_dir())
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, "summary_prompt.txt")
        if not os.path.isfile(prompt_file):
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write("Tu es un assistant.")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            return {"success": True, "output": "Résumé de secours", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path)
        result = runner.run_summary(str(tmp_path / "quick_transcript.txt"))
        assert result["summary_text"] == "Résumé de secours"

    def test_run_summary_failure_returns_indisponible(self, tmp_path, monkeypatch):
        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            return {"success": False, "error": "opencode timeout", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path)
        result = runner.run_summary("/tmp/transcript.txt")
        assert result["summary_text"] == "Résumé indisponible."

    def test_run_summary_includes_diarization_context(self, tmp_path, monkeypatch):
        (tmp_path / "quick_transcript.txt").write_text("Bonjour", encoding="utf-8")
        (tmp_path / "diarization_context.md").write_text("# Diarization", encoding="utf-8")
        (tmp_path / "job_context.yaml").write_text("meeting: {}", encoding="utf-8")
        prompt_dir = os.path.join(_get_prompts_dir())
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, "summary_prompt.txt")
        if not os.path.isfile(prompt_file):
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write("Tu es un assistant.")

        captured = {}

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            captured["instruction"] = instruction
            (tmp_path / "summary.md").write_text("**Titre suggéré :** Test\n", encoding="utf-8")
            return {"success": True, "output": "", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path)
        result = runner.run_summary(
            str(tmp_path / "quick_transcript.txt"),
            context_path=str(tmp_path / "job_context.yaml"),
            diarization_context_path=str(tmp_path / "diarization_context.md"),
        )
        assert "diarization acoustique" in captured["instruction"] or str(tmp_path / "diarization_context.md") in captured["instruction"]
        assert str(tmp_path / "job_context.yaml") in captured["instruction"]


class TestOpenCodeRunnerRunCorrection:
    def test_run_correction_instruction_uses_explicit_input_paths(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "context").mkdir()
        srt_path = tmp_path / "metadata" / "transcription.srt"
        context_path = tmp_path / "context" / "job_context.yaml"
        lexicon_path = tmp_path / "context" / "session_lexicon.json"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:05,000\nBonjour\n", encoding="utf-8")
        context_path.write_text("meeting: {}\n", encoding="utf-8")
        lexicon_path.write_text("[]\n", encoding="utf-8")
        captured = {}

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            captured["instruction"] = instruction
            return {"success": True, "output": "OK", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        runner.run_correction(str(srt_path), str(context_path), str(lexicon_path))

        assert str(srt_path) in captured["instruction"]
        assert str(context_path) in captured["instruction"]
        assert str(lexicon_path) in captured["instruction"]
        assert "lexique validé par l'utilisateur" in captured["instruction"]
        # Sans invite : aucune clause de brief.
        assert "brief d'invitation" not in captured["instruction"].casefold()

    def test_run_correction_adds_invite_spelling_clause_when_present(self, tmp_path, monkeypatch):
        invite_path = tmp_path / "summary" / "meeting_invite.md"
        invite_path.parent.mkdir(parents=True)
        invite_path.write_text("# Brief\n## Documents présentés\nKubernetes\n", encoding="utf-8")
        captured = {}

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            captured["instruction"] = instruction
            return {"success": True, "output": "OK", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        runner.run_correction("/srt", "/ctx", "/lex", str(invite_path))

        instr = captured["instruction"]
        assert str(invite_path) in instr
        assert "orthographe" in instr.casefold()
        # Garde-fou : cadré comme référence, jamais autorité de contenu.
        assert "jamais" in instr.casefold()

    def test_run_correction_ignores_missing_invite_path(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            captured["instruction"] = instruction
            return {"success": True, "output": "OK", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)
        runner = _make_runner(tmp_path / "metadata")
        runner.run_correction("/srt", "/ctx", "/lex", str(tmp_path / "absent.md"))
        assert "brief d'invitation" not in captured["instruction"].casefold()

    def test_run_correction_reads_corrected_srt(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "metadata" / "transcription.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nBonjour\n", encoding="utf-8")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            (tmp_path / "metadata" / "transcription_corrigee.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nBonjour corrigé\n", encoding="utf-8")
            (tmp_path / "metadata" / "correction_report.md").write_text("# Rapport\n2 corrections appliquées\n", encoding="utf-8")
            return {"success": True, "output": "OK", "files": [], "events_count": 2, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        result = runner.run_correction(
            str(tmp_path / "metadata" / "transcription.srt"),
            str(tmp_path / "context" / "job_context.yaml"),
            str(tmp_path / "context" / "session_lexicon.json"),
        )
        assert result["success"] is True
        assert "corrigé" in result["corrected_srt"]
        assert "Rapport" in result["report"]

    def test_run_correction_missing_srt_returns_error(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "metadata" / "transcription.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nBonjour\n", encoding="utf-8")

        def fake_run(self, instruction, prompt_file, timeout=600):
            return {"success": False, "error": "opencode introuvable: /usr/local/bin/opencode", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        result = runner.run_correction(
            str(tmp_path / "metadata" / "transcription.srt"),
            str(tmp_path / "context" / "job_context.yaml"),
            str(tmp_path / "context" / "session_lexicon.json"),
        )
        assert result["success"] is False

    def test_run_correction_opencode_failure(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "metadata" / "transcription.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nTest\n", encoding="utf-8")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            return {"success": False, "error": "opencode exit 1: crash", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        result = runner.run_correction(
            str(tmp_path / "metadata" / "transcription.srt"),
            str(tmp_path / "context" / "job_context.yaml"),
            str(tmp_path / "context" / "session_lexicon.json"),
        )
        assert result["success"] is False
        assert result["corrected_srt"] == ""
        assert result["report"] == ""

    def test_run_correction_no_files_produced(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "metadata" / "transcription.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nTest\n", encoding="utf-8")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            return {"success": True, "output": "Correction terminée", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        result = runner.run_correction(
            str(tmp_path / "metadata" / "transcription.srt"),
            str(tmp_path / "context" / "job_context.yaml"),
            str(tmp_path / "context" / "session_lexicon.json"),
        )
        assert result["success"] is True
        assert result["corrected_srt"] == ""
        assert result["report"] == ""

    def test_run_correction_timeout_with_partial_files_returns_success(self, tmp_path, monkeypatch):
        (tmp_path / "metadata").mkdir()
        (tmp_path / "metadata" / "transcription.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nTest\n", encoding="utf-8")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            (tmp_path / "metadata" / "transcription_corrigee.srt").write_text(
                "1\n00:00:00,000 --> 00:00:05,000\nTest corrigé\n",
                encoding="utf-8",
            )
            (tmp_path / "metadata" / "correction_report.md").write_text(
                "# Rapport\nFichier écrit avant timeout\n",
                encoding="utf-8",
            )
            return {"success": False, "error": "opencode timeout après 900s", "files": [], "events_count": 0, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path / "metadata")
        result = runner.run_correction(
            str(tmp_path / "metadata" / "transcription.srt"),
            str(tmp_path / "context" / "job_context.yaml"),
            str(tmp_path / "context" / "session_lexicon.json"),
        )
        assert result["success"] is True
        assert "corrigé" in result["corrected_srt"]
        assert "timeout" in result["warning"].lower()


class TestRunSummaryInviteInstruction:
    """La clause « brief d'invitation » n'apparaît que si un fichier d'invite existe."""

    def _capture_instruction(self, runner, monkeypatch):
        captured = {}

        def fake_run(instruction, prompt_file, timeout=600):
            captured["instruction"] = instruction
            return {"success": False, "error": "stub", "output": "", "events": 0}

        monkeypatch.setattr(runner, "run", fake_run)
        return captured

    def test_invite_clause_present_when_file_exists(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        invite = tmp_path / "meeting_invite.md"
        invite.write_text("# Brief d'invitation\n", encoding="utf-8")
        captured = self._capture_instruction(runner, monkeypatch)

        runner.run_summary(str(tmp_path / "t.txt"), invite_path=str(invite))

        assert "brief d'invitation" in captured["instruction"].lower()
        assert "INDICATIF" in captured["instruction"]
        assert str(invite) in captured["instruction"]

    def test_no_invite_clause_when_path_is_none(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        captured = self._capture_instruction(runner, monkeypatch)

        runner.run_summary(str(tmp_path / "t.txt"))

        assert "brief d'invitation" not in captured["instruction"].lower()

    def test_no_invite_clause_when_file_missing(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        captured = self._capture_instruction(runner, monkeypatch)

        runner.run_summary(str(tmp_path / "t.txt"), invite_path=str(tmp_path / "nope.md"))

        assert "brief d'invitation" not in captured["instruction"].lower()


class TestStripRoleGender:
    """Le genre recopié par la LLM dans le rôle est retiré (champ dédié ailleurs)."""

    def test_strips_trailing_label_and_symbol(self):
        assert OpenCodeRunner._strip_role_gender(
            "présente les tickets et la salle de conseil. Masculin ♂"
        ) == "présente les tickets et la salle de conseil."

    def test_strips_feminine(self):
        assert OpenCodeRunner._strip_role_gender("intervient sur RH Féminin ♀") == "intervient sur RH"

    def test_strips_dash_separated_gender(self):
        assert OpenCodeRunner._strip_role_gender("anime la revue — Masculin") == "anime la revue"

    def test_strips_parenthesised_gender(self):
        assert OpenCodeRunner._strip_role_gender("rôle (Féminin)") == "rôle"

    def test_strips_standalone_symbol(self):
        assert OpenCodeRunner._strip_role_gender("présente le budget ♂") == "présente le budget"

    def test_keeps_role_without_gender(self):
        role = "présente l'infrastructure serveur"
        assert OpenCodeRunner._strip_role_gender(role) == role

    def test_does_not_eat_word_masculine_inside(self):
        # « masculin » au milieu d'une phrase n'est pas en fin → conservé.
        role = "évoque le vestiaire masculin du bâtiment"
        assert OpenCodeRunner._strip_role_gender(role) == role

    def test_parse_summary_removes_gender_from_role(self):
        text = (
            "## Participants probables\n"
            "- SPEAKER_00 [Didier] : présente les tickets. Masculin ♂\n"
            "- SPEAKER_01 [Marie] : pilote ProWeb Féminin ♀\n"
        )
        result = OpenCodeRunner._parse_structured_summary(text)
        assert "Masculin" not in result["participants_detectes"]
        assert "♂" not in result["participants_detectes"]
        assert "Féminin" not in result["participants_detectes"]
        roles = result.get("speaker_roles", {})
        assert roles["SPEAKER_00"]["role"] == "présente les tickets."
        assert roles["SPEAKER_01"]["role"] == "pilote ProWeb"


class TestBuildHarmonizationGlossary:
    def test_names_and_terms_with_variants(self):
        from transcria.gpu.opencode_runner import build_harmonization_glossary
        g = build_harmonization_glossary(
            [{"name": "Jean Dupont"}, {"name": "Marie Martin"}],
            [{"term": "ACRO", "variants": ["AKRO"]},
             {"term": "ProWeb", "replace_by": "", "variants": ["pro-web", "ProWebs"]}],
        )
        assert "## Noms de participants (orthographe validée)" in g
        assert "- Jean Dupont" in g and "- Marie Martin" in g
        assert "## Termes métier (forme validée ← variantes connues)" in g
        assert "- ACRO ← AKRO" in g
        assert "- ProWeb ← pro-web, ProWebs" in g

    def test_replace_by_takes_precedence_over_term(self):
        from transcria.gpu.opencode_runner import build_harmonization_glossary
        g = build_harmonization_glossary([], [{"term": "tikeo", "replace_by": "Tickéo", "variants": []}])
        assert "- Tickéo" in g and "tikeo" not in g

    def test_empty_inputs_return_empty(self):
        from transcria.gpu.opencode_runner import build_harmonization_glossary
        assert build_harmonization_glossary([], []) == ""
        assert build_harmonization_glossary(None, None) == ""

    def test_dedup_names(self):
        from transcria.gpu.opencode_runner import build_harmonization_glossary
        g = build_harmonization_glossary([{"name": "Jean Dupont"}, {"name": "Jean Dupont"}], [])
        assert g.count("- Jean Dupont") == 1



class TestRunFinalReviewInstruction:
    def _capture(self, runner, monkeypatch, outputs=None):
        captured = {}
        outputs = {
            "summary_harmonized.md": "# Synthèse\nACRO à 90 %.",
            "transcription_reviewed.srt": "1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00: ok\n",
            "structured_data_reviewed.json": '{"decisions": []}',
            "final_review_report.md": "## Synthèse harmonisée\nok",
        } if outputs is None else outputs

        def fake_run(instruction, prompt_file, timeout=600):
            captured["instruction"] = instruction
            captured["prompt_file"] = prompt_file
            for name, content in outputs.items():
                (runner.work_dir / name).write_text(content, encoding="utf-8")
            return {"success": True, "output": "", "events": 0}

        monkeypatch.setattr(runner, "run", fake_run)
        return captured

    def test_instruction_references_all_inputs_and_reads_outputs(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        captured = self._capture(runner, monkeypatch)
        res = runner.run_final_review(
            str(tmp_path / "c.srt"), str(tmp_path / "s.md"),
            str(tmp_path / "g.md"), str(tmp_path / "sd.json"),
        )
        assert captured["prompt_file"].endswith("final_review_prompt.txt")
        for p in ("c.srt", "s.md", "g.md", "sd.json"):
            assert str(tmp_path / p) in captured["instruction"]
        assert res["success"] is True
        assert "ACRO" in res["harmonized_summary"]
        assert res["reviewed_srt"]
        assert res["reviewed_structured_data"] == '{"decisions": []}'
        assert res["report"]

    def test_failure_when_no_output(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        self._capture(runner, monkeypatch, outputs={})
        res = runner.run_final_review(
            str(tmp_path / "c.srt"), str(tmp_path / "s.md"),
            str(tmp_path / "g.md"), str(tmp_path / "sd.json"),
        )
        assert res["success"] is False
        assert res["harmonized_summary"] == ""

    def test_partial_output_logs_warning(self, tmp_path, monkeypatch, caplog):
        """Observabilité (incident 6f4f4cad) : 2/4 fichiers produits → WARNING explicite
        nommant les manquants, au lieu d'un « succès » silencieux."""
        import logging
        runner = _make_runner(tmp_path)
        self._capture(runner, monkeypatch, outputs={
            "summary_harmonized.md": "# Synthèse",
            "transcription_reviewed.srt": "1\n00:00:00,000 --> 00:00:01,000\nx\n",
        })
        with caplog.at_level(logging.WARNING):
            res = runner.run_final_review(
                str(tmp_path / "c.srt"), str(tmp_path / "s.md"),
                str(tmp_path / "g.md"), str(tmp_path / "sd.json"),
            )
        assert res["success"] is True  # best-effort : 2/4 exploités
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "2/4 fichiers" in msgs
        assert "structured_data_reviewed.json" in msgs
        assert "final_review_report.md" in msgs


class TestApplyFinalReview:
    def test_applies_outputs_with_guards(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-fr")
        fs.save_text("metadata/transcription_corrigee.srt",
                     "1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00: AKRO\n")
        fs.save_json("context/meeting_context.json", {"summary_llm": "x"})
        result = {
            "reviewed_srt": "1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00: ACRO\n",
            "harmonized_summary": "# Synthèse\nACRO",
            "reviewed_structured_data": '{"decisions": ["[À VÉRIFIER] budget 60 000 €"]}',
            "report": "## rapport",
        }
        applied = WorkflowRunner._apply_final_review(fs, result)
        assert applied["srt_updated"] and applied["summary_harmonized"] and applied["structured_data_updated"]
        srt = fs.load_text("metadata/transcription_corrigee.srt")
        assert "ACRO" in srt and "AKRO" not in srt
        ctx = fs.load_json("context/meeting_context.json")
        assert ctx["summary_harmonized"] == "# Synthèse\nACRO"
        assert ctx["structured_data"]["decisions"][0].startswith("[À VÉRIFIER]")
        assert (fs.job_dir / "metadata" / "final_review_report.md").exists()

    def test_srt_rejected_when_ratio_off(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-fr2")
        original = "1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00: " + ("mot " * 50) + "\n"
        fs.save_text("metadata/transcription_corrigee.srt", original)
        fs.save_json("context/meeting_context.json", {})
        applied = WorkflowRunner._apply_final_review(fs, {"reviewed_srt": "1\nx\n"})
        assert applied["srt_updated"] is False
        assert fs.load_text("metadata/transcription_corrigee.srt") == original

    def test_invalid_structured_data_kept(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-fr3")
        fs.save_json("context/meeting_context.json", {"structured_data": {"decisions": ["ok"]}})
        applied = WorkflowRunner._apply_final_review(fs, {"reviewed_structured_data": "{not json"})
        assert applied["structured_data_updated"] is False
        assert fs.load_json("context/meeting_context.json")["structured_data"]["decisions"] == ["ok"]


class TestRunErrorEventSurfaced:
    """L'erreur opencode arrive comme événement JSON sur stdout (pas stderr) :
    le message d'erreur doit la refléter, pas rester vide (incident du 16/06/2026)."""

    def test_error_event_on_stdout_becomes_message(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        err = json.dumps({"type": "error", "error": {
            "name": "UnknownError",
            "data": {"message": "Unexpected server error. Check server logs for details."}}})
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(stdout=err, stderr="", returncode=1))

        runner = _make_runner(tmp_path)
        result = runner.run("instr", "/tmp/prompt.txt")
        assert result["success"] is False
        assert result["error_kind"] == "opencode_error"
        assert "UnknownError" in result["error"]
        assert "Unexpected server error" in result["error"]


class TestSummaryFailureClassification:
    """run_summary distingue opencode_error (exit≠0) de empty_output (exit 0, 0 texte)."""

    def test_failure_kind_opencode_error_when_run_fails(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        monkeypatch.setattr(runner, "run", lambda *a, **k: {
            "success": False, "error_kind": "opencode_error",
            "error": "opencode exit 1: UnknownError: Unexpected server error"})
        parsed = runner.run_summary("/tmp/transcript.txt")
        assert parsed["_summary_produced"] is False
        assert parsed["_failure_kind"] == "opencode_error"
        assert "UnknownError" in parsed["_failure_detail"]

    def test_failure_kind_empty_output_when_exit0_no_text(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        monkeypatch.setattr(runner, "run", lambda *a, **k: {"success": True, "output": "", "files": []})
        parsed = runner.run_summary("/tmp/transcript.txt")
        assert parsed["_summary_produced"] is False
        assert parsed["_failure_kind"] == "empty_output"


class TestWatchdogVllmSignal:
    """_llm_is_processing sonde llama.cpp (/slots) PUIS vLLM (/metrics) — durcissement split."""

    def test_vllm_metrics_busy_parse(self):
        busy = 'vllm:num_requests_running{model="q"} 1.0\nvllm:num_requests_waiting{model="q"} 0.0'
        idle = 'vllm:num_requests_running{model="q"} 0.0\nvllm:num_requests_waiting{model="q"} 0.0'
        waiting = 'vllm:num_requests_running 0.0\nvllm:num_requests_waiting 3.0'
        assert OpenCodeRunner._vllm_metrics_busy(busy) is True
        assert OpenCodeRunner._vllm_metrics_busy(idle) is False
        assert OpenCodeRunner._vllm_metrics_busy(waiting) is True     # requêtes en attente = occupé
        assert OpenCodeRunner._vllm_metrics_busy("python_gc 5") is None  # pas du vLLM

    def test_fallback_slots_absent_utilise_metrics_vllm(self, tmp_path, monkeypatch):
        import requests
        runner = _make_runner(tmp_path, config={"workflow": {"arbitration_llm": {"model_id": "local/q"}}})

        class _Resp:
            def __init__(self, code, text=""):
                self.status_code = code
                self._text = text
            @property
            def text(self):
                return self._text
            def json(self):
                raise ValueError("pas de JSON")

        def _fake_get(url, timeout=0):
            if url.endswith("/slots"):
                return _Resp(404)                       # vLLM n'a pas /slots
            if url.endswith("/metrics"):
                return _Resp(200, "vllm:num_requests_running 2.0\nvllm:num_requests_waiting 0.0")
            return _Resp(404)

        monkeypatch.setattr(requests, "get", _fake_get)
        assert runner._llm_is_processing() is True       # détecté via /metrics vLLM

    def test_aucune_sonde_renvoie_none(self, tmp_path, monkeypatch):
        import requests
        runner = _make_runner(tmp_path, config={"workflow": {"arbitration_llm": {"model_id": "local/q"}}})
        monkeypatch.setattr(requests, "get",
                            lambda url, timeout=0: type("R", (), {"status_code": 404, "text": ""})())
        assert runner._llm_is_processing() is None       # ni /slots ni /metrics → repli idle pur

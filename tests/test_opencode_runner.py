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
    """Simule subprocess.Popen pour les tests de OpenCodeRunner."""
    def __init__(self, stdout="", stderr="", returncode=0, communicate_exc=None):
        self.pid = 99999
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._communicate_exc = communicate_exc

    def communicate(self, timeout=None):
        if self._communicate_exc is not None:
            raise self._communicate_exc
        return self._stdout, self._stderr

    def send_signal(self, sig):
        pass

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

    def test_run_timeout(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/opencode")
        monkeypatch.setattr(os.path, "isfile", lambda p: True)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        monkeypatch.setattr(subprocess, "Popen",
            _fake_popen(communicate_exc=subprocess.TimeoutExpired(cmd=[], timeout=600)))

        runner = _make_runner(tmp_path)
        result = runner.run("test instruction", "/tmp/prompt.txt", timeout=600)
        assert result["success"] is False
        assert "timeout" in result["error"].lower()

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

- SPEAKER_00 [Sylvain Martin] : personne s'identifiant dans un extrait vocal (rôle non identifiable au-delà de l'auto-désignation)
"""
        result = OpenCodeRunner._parse_structured_summary(text)

        assert "Sylvain Martin" in result["participants_detectes"]
        assert result["speaker_roles"]["SPEAKER_00"] == {
            "label": "Sylvain Martin",
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
        (tmp_path / "summary.md").write_text("# Résumé\n\n**Titre suggéré :** Mon titre\n", encoding="utf-8")
        (tmp_path / "quick_transcript.txt").write_text("[0s->1s] Bonjour", encoding="utf-8")
        prompt_dir = os.path.join(_get_prompts_dir())
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, "summary_prompt.txt")
        if not os.path.isfile(prompt_file):
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write("Tu es un assistant.")

        def fake_run(self, instruction, prompt_file_arg, timeout=600):
            return {"success": True, "output": "Résumé généré", "files": [], "events_count": 1, "tool_calls": 0}

        monkeypatch.setattr(OpenCodeRunner, "run", fake_run)

        runner = _make_runner(tmp_path)
        result = runner.run_summary(str(tmp_path / "quick_transcript.txt"))
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

"""Panne opencode reconnue → cause nommée → remède automatique (2026-11-07).

Signature réelle : opencode v1 1.18.22 face à `mistral/zai-glm-5-3` — `AI_TypeValidationError`
sur `choices[0].delta.tool_calls[0].id` à chaque appel d'outil, trois tentatives perdues et un
bandeau qui accusait « modèle non résolu ou serveur en erreur ». La v2 passait.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from transcria.llm_tools import opencode_remediation as R
from transcria.llm_tools.opencode_cli import OpencodeVersion

REAL_STDERR = (
    'timestamp=2026-11-07T20:53:28.515Z level=ERROR run=983fea1f message=process session.id=ses_x '
    'error="Type validation failed: Value: {"id":"f25b4bb7","object":"chat.completion.chunk","model":"zai-glm-5-3",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"function":{"name":"","arguments":"cria-agent-work/fd"},"index":0}],'
    '"content":""},"finish_reason":null}]}.\\nError message: [\\n  {\\n    "expected": "string",\\n    "code": "invalid_type",'
    '\\n    "path": [\\n      "choices",\\n      0,\\n      "delta",\\n      "tool_calls",\\n      0,\\n      "id"\\n    ],'
    '\\n    "message": "Invalid input: expected string, received undefined"\\n  }\\n]"'
)


# ── 1. reconnaître ───────────────────────────────────────────────────────────

def test_signature_reelle_reconnue_dans_le_stderr():
    assert R.classify_failure(REAL_STDERR, []) == R.SDK_TOOL_CALL_INCOMPAT


def test_signature_reconnue_dans_un_evenement_json_echappe():
    event = {"type": "error", "error": {"name": "AI_TypeValidationError",
             "message": 'Type validation failed: … "delta":{"tool_calls":[{"function":{}}]} … '
                        '"path":["choices",0,"delta","tool_calls",0,"id"], "message":"expected string, received undefined"'}}
    assert R.classify_failure("", [event]) == R.SDK_TOOL_CALL_INCOMPAT
    assert "tool_calls" in json.dumps(event)


@pytest.mark.parametrize("text", [
    "", "opencode exit 1: ModelUnavailableError: mistral/x", "provider.auth: 401 Unauthorized",
    "Type validation failed: choices[0].message.content expected string, received undefined",   # pas un appel d'outil
    "tool_calls id received undefined",                                                          # sans la validation du SDK
])
def test_pannes_generiques_non_classees(text):
    assert R.classify_failure(text, [{"type": "text"}]) is None


def test_cause_nommee_sans_cle_ni_jargon():
    txt = R.cause_text(R.SDK_TOOL_CALL_INCOMPAT, version=OpencodeVersion(1, 18, 22, raw="1.18.22"))
    assert "v1.18.22" in txt and "appels d'outil" in txt and "LLM d'arbitrage" in txt
    assert "clé" in txt and "api" not in txt.lower().replace("l'api", "")
    assert R.cause_text("autre") == "autre"


# ── 2. chercher un binaire v2 de confiance ───────────────────────────────────

def _bin(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_candidats_dedoublonnes_et_ordonnes(tmp_path):
    home = tmp_path / "home"
    a = _bin(home / ".opencode" / "bin" / "opencode")
    pathdir = tmp_path / "bin"
    link = pathdir / "opencode"
    pathdir.mkdir()
    os.symlink(a, link)                                        # même fichier réel que `a`
    found = R.candidate_binaries(home=str(home), path_env=str(pathdir), globber=lambda pat: [])
    reals = [os.path.realpath(p) for p in found]
    assert reals.count(os.path.realpath(a)) == 1                 # dédoublonné par chemin réel
    assert found[0] == str(link)                                 # le PATH d'abord


def test_confiance_proprietaire_droits_et_emplacement(tmp_path):
    ok = _bin(tmp_path / "ok" / "opencode")
    group_writable = _bin(tmp_path / "gw" / "opencode", 0o775)
    not_exec = _bin(tmp_path / "nx" / "opencode", 0o644)
    me = {os.geteuid()}
    assert R.is_trusted_binary(str(ok), me)
    assert not R.is_trusted_binary(str(group_writable), me)       # modifiable par d'autres
    assert not R.is_trusted_binary(str(not_exec), me)
    assert not R.is_trusted_binary(str(ok), {999999})              # propriétaire étranger
    assert not R.is_trusted_binary(str(tmp_path / "absent"), me)
    assert not R.is_trusted_binary(str(tmp_path / "ok"), me)       # un répertoire n'est pas un binaire


def test_trusted_uids_inclut_le_proprietaire_de_la_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    uids = R.trusted_uids(str(cfg))
    assert 0 in uids and os.geteuid() in uids and cfg.stat().st_uid in uids


def test_find_v2_ignore_le_binaire_en_cause_et_les_v1(tmp_path):
    home = tmp_path / "home"
    v1 = _bin(home / ".npm-global" / "bin" / "opencode")
    v2 = _bin(home / ".opencode" / "bin" / "opencode")
    versions = {str(v1): OpencodeVersion(1, 19, 5, raw="1.19.5"), str(v2): OpencodeVersion(2, 2, 6, raw="2.2.6")}
    reader = lambda p: versions.get(p, OpencodeVersion(0))  # noqa: E731
    cands = [str(v1), str(v2)]
    found = R.find_v2_binary(exclude=str(v1), version_reader=reader, candidates=cands, config_path=str(tmp_path / "config.yaml"))
    assert found and found[0] == str(v2) and found[1].line == 2
    # le v2 lui-même en cause (déjà en v2) ⇒ rien d'autre
    assert R.find_v2_binary(exclude=str(v2), version_reader=reader, candidates=cands, config_path=str(tmp_path / "config.yaml")) is None


def test_find_v2_ne_lit_pas_la_version_d_un_fichier_non_fiable(tmp_path):
    home = tmp_path / "home"
    shady = _bin(home / ".opencode" / "bin" / "opencode", 0o777)
    executed: list[str] = []

    def reader(p):
        executed.append(p)
        return OpencodeVersion(2, 2, 6, raw="2.2.6")

    assert R.find_v2_binary(exclude=None, version_reader=reader, candidates=[str(shady)],
                            config_path=str(tmp_path / "config.yaml")) is None
    assert executed == []                                        # jamais exécuté


# ── 3. basculer : disque + mémoire, trace annulable ──────────────────────────

_YAML = """workflow:
  arbitration_llm:
    enabled: true
    model_id: mistral/zai-glm-5-3
    opencode_bin: /usr/local/sbin/opencode
services:
  backend: cloud
"""


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(R, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("pas de singleton")))
    return path


def test_switch_ecrit_le_yaml_la_memoire_et_l_enregistrement(cfg_file, caplog):
    import logging

    import yaml
    config = {"workflow": {"arbitration_llm": {"opencode_bin": "/usr/local/sbin/opencode"}}}
    with caplog.at_level(logging.WARNING, logger="transcria.llm_tools.opencode_remediation"):
        rec = R.switch_binary(config, "/home/admin/.opencode/bin/opencode", previous="/usr/local/sbin/opencode",
                              reason="sdk_tool_call_incompat (v1.18.22 → v2.2.5)", config_path=str(cfg_file))
    on_disk = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["workflow"]["arbitration_llm"]
    assert on_disk["opencode_bin"] == "/home/admin/.opencode/bin/opencode"
    assert on_disk["opencode_bin_previous"] == "/usr/local/sbin/opencode"
    assert on_disk["opencode_bin_switch_reason"].startswith("sdk_tool_call_incompat")
    assert on_disk["model_id"] == "mistral/zai-glm-5-3" and on_disk["enabled"] is True   # diff minimal
    assert config["workflow"]["arbitration_llm"]["opencode_bin"] == "/home/admin/.opencode/bin/opencode"
    assert rec["persisted"] is True and rec["automatic"] is True and rec["to"].endswith("opencode")
    assert any("basculé automatiquement" in r.getMessage() for r in caplog.records)   # jamais silencieux
    pending = R.pending_switch(yaml.safe_load(cfg_file.read_text(encoding="utf-8")))
    assert pending and pending["from"] == "/usr/local/sbin/opencode" and pending["at"]


def test_switch_sans_config_yaml_reste_effectif_en_memoire(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "get_config", lambda: None)
    config = {"workflow": {"arbitration_llm": {"opencode_bin": "a"}}}
    rec = R.switch_binary(config, "b", previous="a", reason="r", config_path=str(tmp_path / "absent" / "config.yaml"))
    assert rec["persisted"] is False and config["workflow"]["arbitration_llm"]["opencode_bin"] == "b"


def test_revert_retablit_et_efface_la_trace(cfg_file):
    import yaml
    config = {"workflow": {"arbitration_llm": {"opencode_bin": "/usr/local/sbin/opencode"}}}
    R.switch_binary(config, "/v2/opencode", previous="/usr/local/sbin/opencode", reason="r", config_path=str(cfg_file))
    assert R.revert_switch(config, config_path=str(cfg_file)) == "/usr/local/sbin/opencode"
    on_disk = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["workflow"]["arbitration_llm"]["opencode_bin"] == "/usr/local/sbin/opencode"
    assert R.pending_switch(on_disk) is None and R.pending_switch(config) is None
    assert R.revert_switch(config, config_path=str(cfg_file)) is None          # plus rien à annuler


def test_auto_switch_seulement_pour_la_panne_reconnue_depuis_une_v1(cfg_file, tmp_path, monkeypatch):
    v2 = _bin(tmp_path / "home" / ".opencode" / "bin" / "opencode")
    monkeypatch.setattr(R, "candidate_binaries", lambda **kw: [str(v2)])
    reader = lambda p: OpencodeVersion(2, 2, 6, raw="2.2.6")  # noqa: E731
    config = {"workflow": {"arbitration_llm": {"opencode_bin": "/v1/opencode"}}}
    v1 = OpencodeVersion(1, 18, 22, raw="1.18.22")
    assert R.auto_switch_for(config, current_bin="/v1/opencode", current_version=v1, kind="opencode_error",
                             version_reader=reader, config_path=str(cfg_file)) is None
    assert R.auto_switch_for(config, current_bin="/v2/x", current_version=OpencodeVersion(2, 2, 5, raw="2.2.5"),
                             kind=R.SDK_TOOL_CALL_INCOMPAT, version_reader=reader, config_path=str(cfg_file)) is None
    rec = R.auto_switch_for(config, current_bin="/v1/opencode", current_version=v1, kind=R.SDK_TOOL_CALL_INCOMPAT,
                            version_reader=reader, config_path=str(cfg_file))
    assert rec and rec["to"] == str(v2) and "v1.18.22 → v2.2.6" in rec["reason"]
    assert config["workflow"]["arbitration_llm"]["opencode_bin"] == str(v2)


def test_auto_switch_sans_v2_nomme_la_panne_sans_basculer(cfg_file, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(R, "candidate_binaries", lambda **kw: [])
    config = {"workflow": {"arbitration_llm": {"opencode_bin": "/v1/opencode"}}}
    with caplog.at_level(logging.WARNING, logger="transcria.llm_tools.opencode_remediation"):
        rec = R.auto_switch_for(config, current_bin="/v1/opencode", current_version=OpencodeVersion(1, 18, 22, raw="1.18.22"),
                                kind=R.SDK_TOOL_CALL_INCOMPAT, version_reader=lambda p: OpencodeVersion(0), config_path=str(cfg_file))
    assert rec is None and config["workflow"]["arbitration_llm"]["opencode_bin"] == "/v1/opencode"
    assert any("aucun binaire v2 de confiance" in r.getMessage() for r in caplog.records)
    assert stat.S_ISREG(cfg_file.stat().st_mode)

"""Écriture ciblée du plan multi-instance STT — round-trip ruamel, conservation."""
from __future__ import annotations

import pytest

from transcria.config.stt_instances_config import apply_stt_instances

_BASE = """\
# commentaire préservé
server:
  port: 7870
resource_node:
  engines:
  - {name: cohere, script: scripts/launch_stt_cohere.sh, gpu: 0, port: 8003}
  - {name: qwen3asr, script: scripts/launch_stt_qwen3asr.sh, gpu: 1, port: 8021}
inference:
  mode: hybrid
  stt:
    backends:
      qwen3asr: {url: http://127.0.0.1:8021/v1, model: qwen3-asr-1.7b}
"""


def _apply(path):
    apply_stt_instances(
        path, backend="qwen3asr",
        engines=[
            {"name": "qwen3asr", "script": "scripts/launch_stt_qwen3asr.sh",
             "gpu": 1, "gpu_mem": 0.15, "port": 8021, "idle_timeout_s": 900},
            {"name": "qwen3asr-2", "backend": "qwen3asr",
             "script": "scripts/launch_stt_qwen3asr.sh",
             "gpu": 1, "gpu_mem": 0.15, "port": 8022, "idle_timeout_s": 900},
        ],
        url="http://127.0.0.1:8021/v1",
        extra_urls=["http://127.0.0.1:8022/v1"],
        concurrency=4,
    )


def test_applique_le_plan_et_conserve_le_reste(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE, encoding="utf-8")

    _apply(cfg)

    text = cfg.read_text(encoding="utf-8")
    assert "# commentaire préservé" in text          # round-trip ruamel
    assert "port: 7870" in text

    import yaml as pyyaml
    data = pyyaml.safe_load(text)
    names = [e["name"] for e in data["resource_node"]["engines"]]
    assert names == ["cohere", "qwen3asr", "qwen3asr-2"]  # cohere intact, qwen3asr remplacé
    spec = data["inference"]["stt"]["backends"]["qwen3asr"]
    assert spec["extra_urls"] == ["http://127.0.0.1:8022/v1"]
    assert spec["model"] == "qwen3-asr-1.7b"              # clé existante conservée
    assert data["inference"]["stt"]["concurrency"] == 4


def test_reapplication_idempotente(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE, encoding="utf-8")
    _apply(cfg)
    first = cfg.read_text(encoding="utf-8")
    _apply(cfg)
    assert cfg.read_text(encoding="utf-8") == first


def test_retour_mono_instance_retire_extra_urls(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE, encoding="utf-8")
    _apply(cfg)
    apply_stt_instances(
        cfg, backend="qwen3asr",
        engines=[{"name": "qwen3asr", "script": "scripts/launch_stt_qwen3asr.sh",
                  "gpu": 1, "gpu_mem": 0.15, "port": 8021, "idle_timeout_s": 900}],
        url="http://127.0.0.1:8021/v1", extra_urls=[], concurrency=1,
    )
    import yaml as pyyaml
    data = pyyaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "extra_urls" not in data["inference"]["stt"]["backends"]["qwen3asr"]


def test_erreurs_franches(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_stt_instances(tmp_path / "absent.yaml", backend="x",
                            engines=[{"name": "x"}], url="u", extra_urls=[], concurrency=1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE, encoding="utf-8")
    with pytest.raises(ValueError):
        apply_stt_instances(cfg, backend="x", engines=[], url="u", extra_urls=[], concurrency=1)

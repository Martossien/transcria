"""Écriture de la calibration GPU (transcria.config.gpu_calibration).

Vérifie le contrat audit : on met à jour SEULEMENT les 3 clés de calibration, on
PRÉSERVE le reste (commentaires, autres clés, secrets), et on gère le format liste
par blocs (`- 0`) — là où le `sed` de switch_arbitrage_llm.sh échoue silencieusement.
"""
from __future__ import annotations

import pytest

from transcria.config.gpu_calibration import apply_gpu_calibration

SAMPLE = """\
# Configuration TranscrIA (NE PAS COMMITER — secrets)
database:
  password: s3cr3t_ne_pas_perdre  # commentaire sensible
gpu:
  cohere_vram_mb: 6000
  llm_vram_mb: 60000
  llm_gpu_indices:
  - 0
  - 1
  - 2
  min_free_vram_mb: 4000
services:
  arbitrage_llm_port: 8080
"""


def _write(tmp_path, text=SAMPLE):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_updates_only_the_three_keys_and_preserves_the_rest(tmp_path):
    p = _write(tmp_path)
    apply_gpu_calibration(p, vram_mb=49000, gpu_indices=[0, 1], vram_mb_per_gpu=[26000, 23000])

    import yaml

    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["gpu"]["llm_vram_mb"] == 49000
    assert cfg["gpu"]["llm_gpu_indices"] == [0, 1]
    assert cfg["gpu"]["llm_vram_mb_per_gpu"] == [26000, 23000]
    # Reste préservé
    assert cfg["gpu"]["cohere_vram_mb"] == 6000
    assert cfg["gpu"]["min_free_vram_mb"] == 4000
    assert cfg["database"]["password"] == "s3cr3t_ne_pas_perdre"
    assert cfg["services"]["arbitrage_llm_port"] == 8080


def test_preserves_comments(tmp_path):
    p = _write(tmp_path)
    apply_gpu_calibration(p, vram_mb=49000, gpu_indices=[0, 1], vram_mb_per_gpu=[26000, 23000])
    text = p.read_text(encoding="utf-8")
    assert "NE PAS COMMITER" in text
    assert "commentaire sensible" in text


def test_adds_per_gpu_when_absent(tmp_path):
    # Le format de départ n'a PAS de llm_vram_mb_per_gpu : il doit être ajouté.
    p = _write(tmp_path)
    assert "llm_vram_mb_per_gpu" not in p.read_text(encoding="utf-8")
    apply_gpu_calibration(p, vram_mb=22300, gpu_indices=[0], vram_mb_per_gpu=[22300])
    import yaml

    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["gpu"]["llm_vram_mb_per_gpu"] == [22300]


def test_creates_gpu_block_if_missing(tmp_path):
    p = _write(tmp_path, "services:\n  arbitrage_llm_port: 8080\n")
    apply_gpu_calibration(p, vram_mb=12700, gpu_indices=[0], vram_mb_per_gpu=[12700])
    import yaml

    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["gpu"]["llm_vram_mb"] == 12700


def test_rejects_inconsistent_arguments(tmp_path):
    p = _write(tmp_path)
    with pytest.raises(ValueError):
        apply_gpu_calibration(p, vram_mb=0, gpu_indices=[0], vram_mb_per_gpu=[0])
    with pytest.raises(ValueError):
        apply_gpu_calibration(p, vram_mb=1000, gpu_indices=[], vram_mb_per_gpu=[])
    with pytest.raises(ValueError):
        apply_gpu_calibration(p, vram_mb=1000, gpu_indices=[0, 1], vram_mb_per_gpu=[1000])


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_gpu_calibration(tmp_path / "nope.yaml", vram_mb=1000, gpu_indices=[0], vram_mb_per_gpu=[1000])

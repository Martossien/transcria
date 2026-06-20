"""Tests unitaires de la phase « résumé final » de l'installateur.

Présentation seule : on vérifie que les blocs (en-tête profil, modèles, base, config,
démarrage) sont rendus en process via les renderers déjà testés, avec le bon profil et
le décompte CHANGE-ME réel. Aucun effet de bord.
"""
from __future__ import annotations

import io
from pathlib import Path

from transcria.installer.console import Console
from transcria.installer.summary_phase import SummaryPlan, apply_summary


def _render(plan: SummaryPlan) -> str:
    out = io.StringIO()
    apply_summary(plan, console=Console(out, color=False))
    return out.getvalue()


def _plan(tmp_path: Path, **kw) -> SummaryPlan:
    config = tmp_path / "config.yaml"
    if not config.exists():
        config.write_text("server:\n  port: 7870\n", encoding="utf-8")
    defaults = dict(
        profile="web",
        install_dir=tmp_path,
        venv=tmp_path / "venv",
        config_path=config,
        inference_log_dir="/var/log",
        final_log_file="/var/log/transcrIA.log",
        db_backend="PostgreSQL (db@h:5432)",
        doctor_status="OK",
    )
    defaults.update(kw)
    return SummaryPlan(**defaults)


def test_web_summary_has_header_db_and_next_steps(tmp_path):
    text = _render(_plan(tmp_path, profile="web"))
    assert "Résumé de l'installation" in text
    assert "tier web installé" in text
    assert "PostgreSQL (db@h:5432)" in text
    assert "transcria-web" in text  # commandes de démarrage du profil web


def test_resource_node_summary_uses_inference_lines(tmp_path):
    text = _render(_plan(tmp_path, profile="resource-node", db_backend="SQLite"))
    assert "nœud de ressources" in text.lower() or "Inference Service" in text
    assert "transcria-inference" in text


def test_summary_reports_remaining_change_me(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("a: CHANGE-ME\nb: CHANGE-ME\n", encoding="utf-8")
    text = _render(_plan(tmp_path, config_path=config))
    # render_configuration_summary signale les valeurs par défaut restantes
    assert "2" in text and "config.yaml" in text


def test_summary_missing_config_is_graceful(tmp_path):
    text = _render(_plan(tmp_path, config_path=tmp_path / "absent.yaml"))
    assert "Résumé de l'installation" in text  # pas d'exception sur fichier absent


def test_model_summary_reflects_llm_flags(tmp_path):
    text = _render(_plan(tmp_path, profile="all-in-one", needs_llm=True, qwen_ok=True, opencode_bin="/usr/bin/opencode"))
    assert "/usr/bin/opencode" in text
    assert "LLM d'arbitrage" in text

"""Tests du filet d'architecture (scripts/audit_imports.py — vague A0 du plan qualité).

Deux familles : (1) sur un mini-arbre synthétique, chaque métrique et chaque règle du
ratchet est vérifiée DANS LES DEUX SENS (passe quand c'est propre, échoue quand ça se
dégrade — un filet qui ne rougit jamais ne protège rien) ; (2) sur l'arbre RÉEL, les
invariants durs du plan (zéro cycle, baseline versionnée respectée).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_imports", Path(__file__).resolve().parent.parent / "scripts" / "audit_imports.py"
)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(audit)


def _make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    # les deux racines doivent exister
    for root in audit.ROOTS:
        (tmp_path / root).mkdir(exist_ok=True)
        init = tmp_path / root / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    return tmp_path


class TestMetrics:
    def test_fanout_fanin_and_deferred(self, tmp_path):
        base = _make_tree(tmp_path, {
            "transcria/a.py": "from transcria import b\n\ndef f():\n    from transcria import c\n",
            "transcria/b.py": "",
            "transcria/c.py": "",
        })
        metrics = audit.collect_metrics(base)
        assert metrics["fanout"]["transcria.a"] == 2
        assert metrics["deferred_internal_imports"] == 1
        assert metrics["cycles"] == 0

    def test_cycle_detected(self, tmp_path):
        base = _make_tree(tmp_path, {
            "transcria/a.py": "from transcria import b\n",
            "transcria/b.py": "from transcria import a\n",
        })
        metrics = audit.collect_metrics(base)
        assert metrics["cycles"] >= 1
        assert any("transcria.a" in d for d in metrics["cycles_detail"])

    def test_deferred_import_not_a_top_level_cycle(self, tmp_path):
        # un « cycle » dont une branche est différée n'est PAS un cycle top-level
        base = _make_tree(tmp_path, {
            "transcria/a.py": "from transcria import b\n",
            "transcria/b.py": "def f():\n    from transcria import a\n",
        })
        assert audit.collect_metrics(base)["cycles"] == 0

    def test_deep_chains_and_long_functions(self, tmp_path):
        long_fn = "def f(cfg):\n" + "    x = 1\n" * 151 + '    cfg.get("gpu", {}).get("x")\n'
        base = _make_tree(tmp_path, {"transcria/a.py": long_fn})
        metrics = audit.collect_metrics(base)
        assert metrics["deep_config_chains"] == 1
        assert metrics["functions_over_150"] == 1


class TestRatchet:
    def test_ok_when_equal_and_when_improved(self, tmp_path):
        base = _make_tree(tmp_path, {"transcria/a.py": "from transcria import b\n", "transcria/b.py": ""})
        metrics = audit.collect_metrics(base)
        baseline = {k: v for k, v in metrics.items() if k != "cycles_detail"}
        assert audit.check_baseline(metrics, baseline) == []
        baseline["deferred_internal_imports"] += 5  # marge = amélioration : OK
        assert audit.check_baseline(metrics, baseline) == []

    def test_fails_on_each_regression(self, tmp_path):
        base = _make_tree(tmp_path, {"transcria/a.py": "from transcria import b\n", "transcria/b.py": ""})
        metrics = audit.collect_metrics(base)
        baseline = {k: v for k, v in metrics.items() if k != "cycles_detail"}
        for key in ("deferred_internal_imports", "deep_config_chains", "functions_over_150"):
            worse = dict(metrics)
            worse[key] = baseline.get(key, 0) + 1
            assert audit.check_baseline(worse, baseline), key

    def test_fails_on_fanout_regression_and_new_module_budget(self, tmp_path):
        base = _make_tree(tmp_path, {"transcria/a.py": "from transcria import b\n", "transcria/b.py": ""})
        metrics = audit.collect_metrics(base)
        baseline = {k: v for k, v in metrics.items() if k != "cycles_detail"}
        worse = dict(metrics)
        worse["fanout"] = dict(metrics["fanout"], **{"transcria.a": 2})
        assert audit.check_baseline(worse, baseline)
        # module absent de la baseline : budget nouveau module
        worse["fanout"] = dict(metrics["fanout"], **{"transcria.new": audit.NEW_MODULE_FANOUT_BUDGET + 1})
        assert audit.check_baseline(worse, baseline)

    def test_cycles_always_fail_even_if_baseline_had_some(self, tmp_path):
        base = _make_tree(tmp_path, {
            "transcria/a.py": "from transcria import b\n",
            "transcria/b.py": "from transcria import a\n",
        })
        metrics = audit.collect_metrics(base)
        baseline = {k: v for k, v in metrics.items() if k != "cycles_detail"}
        assert any("CYCLES" in p for p in audit.check_baseline(metrics, baseline))


class TestRealTree:
    """Invariants durs sur le dépôt réel — c'est aussi ce que la CI exécute."""

    @pytest.fixture(scope="class")
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def test_no_toplevel_import_cycles(self, repo_root):
        metrics = audit.collect_metrics(repo_root)
        assert metrics["cycles"] == 0, metrics["cycles_detail"]

    def test_versioned_baseline_is_respected(self, repo_root):
        baseline_path = repo_root / "quality_baseline.json"
        assert baseline_path.exists(), "quality_baseline.json manquante (vague A0)"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        problems = audit.check_baseline(audit.collect_metrics(repo_root), baseline)
        assert problems == [], "\n".join(problems)

    def test_cli_smoke(self, repo_root, capsys):
        assert audit.main(["--base", str(repo_root)]) == 0
        out = capsys.readouterr().out
        assert "cycles top-level          : 0" in out

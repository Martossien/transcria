#!/usr/bin/env python3
"""Audit d'architecture — graphe d'imports internes, différés, cycles, budgets (ratchet).

Outil de la vague A0 du plan `docs/REFACTORING_QUALITE.md`. Trois usages :

    venv/bin/python scripts/audit_imports.py                     # résumé lisible
    venv/bin/python scripts/audit_imports.py --write-baseline quality_baseline.json
    venv/bin/python scripts/audit_imports.py --check-baseline quality_baseline.json

Le mode --check-baseline est le RATCHET exécuté en CI : il échoue (exit 1) si une
métrique s'est DÉGRADÉE par rapport au fichier versionné — on n'exige pas mieux, on
interdit pire. Règles :

- **cycles d'imports top-level** : toujours 0 (le graphe est acyclique — c'est ce qui
  rend les refactorings mécaniques, cf. plan §1) ;
- **imports internes différés** (déclarés dans une fonction) : le total ne peut
  qu'baisser ; les cas légitimes (torch au boot, dépendance optionnelle, point
  d'entrée) sont documentés au §8.3 du plan ;
- **fan-out par module** (nombre de modules internes importés) : un module ne peut pas
  dépasser sa valeur de référence ; un module NOUVEAU respecte le budget (20) ;
- **chaînes de config profondes** `get("a", {}).get(` : le total ne peut que baisser ;
- **fonctions > 150 lignes** : le compte ne peut que monter... non — que BAISSER.

Après une vague qui améliore les chiffres : re-générer la baseline (--write-baseline)
dans le même commit. Toute l'analyse est en AST/regex pures — aucune importation du
code audité, donc exécutable sans dépendances lourdes ni GPU.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOTS = ("transcria", "inference_service")
NEW_MODULE_FANOUT_BUDGET = 20
FUNC_LINES_LIMIT = 150
DEEP_CHAIN_RE = re.compile(r'get\("[a-z_]+", \{\}\)\.get\(')


def iter_modules(base: Path) -> dict[str, Path]:
    """Tous les modules Python des racines auditées, en notation pointée."""
    modules: dict[str, Path] = {}
    for root in ROOTS:
        for path in sorted((base / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            dotted = ".".join(path.relative_to(base).with_suffix("").parts)
            if dotted.endswith(".__init__"):
                dotted = dotted[: -len(".__init__")]
            modules[dotted] = path
    return modules


def _import_targets(node: ast.AST) -> list[str]:
    # ImportFrom : viser `module.alias` (cas `from transcria import b` où b est un
    # sous-module) — la résolution redescendra sur `module` si l'alias n'est qu'un symbole.
    if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ROOTS:
        return [f"{node.module}.{a.name}" for a in node.names]
    if isinstance(node, ast.Import):
        return [a.name for a in node.names if a.name.split(".")[0] in ROOTS]
    return []


def build_graph(modules: dict[str, Path]) -> tuple[dict[str, set[str]], dict[str, int], dict[str, set[str]]]:
    """Retourne (fan-out complet, différés par module, arêtes top-level)."""

    def resolve(name: str) -> str | None:
        while name and name not in modules:
            name = name.rpartition(".")[0]
        return name or None

    fanout: dict[str, set[str]] = defaultdict(set)
    deferred: dict[str, int] = defaultdict(int)
    top_edges: dict[str, set[str]] = defaultdict(set)
    for dotted, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node):
                resolved = resolve(target)
                if resolved is None or resolved == dotted:
                    continue
                fanout[dotted].add(resolved)
                if node.col_offset > 0:
                    deferred[dotted] += 1
                else:
                    top_edges[dotted].add(resolved)
    return fanout, deferred, top_edges


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Cycles du graphe top-level (DFS trois couleurs). Doit rester vide."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for neighbor in sorted(edges.get(node, ())):
            if color[neighbor] == GRAY:
                cycles.append(stack[stack.index(neighbor):] + [neighbor])
            elif color[neighbor] == WHITE:
                dfs(neighbor)
        stack.pop()
        color[node] = BLACK

    for module in sorted(edges):
        if color[module] == WHITE:
            dfs(module)
    return cycles


def find_init_cycles(modules: dict[str, Path], top_edges: dict[str, set[str]]) -> list[str]:
    """Cycles inter-paquets EN COMPTANT les __init__ (vague C5).

    Importer ``a.b.c`` exécute ``a/__init__`` puis ``a/b/__init__`` : le graphe réel
    a des arêtes importeur→ancêtres(cible). Le graphe module-à-module les ignore —
    c'est ce qui a laissé passer les bombes d'ordre d'import gpu↔context et
    workflow↔audio pendant C5. On ne signale que les cycles traversant AU MOINS
    deux paquets : un paquet qui importe ses propres sous-modules est le
    fonctionnement normal de Python.
    """
    def ancestors(mod: str) -> list[str]:
        parts = mod.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in modules]

    edges: dict[str, set[str]] = defaultdict(set)
    for src, targets in top_edges.items():
        for target in targets:
            for hop in ancestors(target):
                if hop != src:
                    edges[src].add(hop)

    def package(mod: str) -> str:
        # Unité de regroupement : les sous-paquets de transcria (transcria.gpu, …) ;
        # inference_service est plat — c'est lui-même l'unité.
        parts = mod.split(".")
        return ".".join(parts[:2]) if parts[0] == "transcria" else parts[0]

    seen: set[frozenset] = set()
    findings: list[str] = []
    for cycle in find_cycles(edges):
        key = frozenset(cycle)
        if key in seen or len({package(m) for m in cycle}) < 2:
            continue
        seen.add(key)
        findings.append(" -> ".join(cycle))
    return findings


def count_routes_missing_docstring(modules: dict[str, Path]) -> int:
    """Routes Flask (décorateur ``.route(...)``) sans docstring — ratchet C8.

    La référence d'API (docs/API_REFERENCE.md) rend la première ligne de docstring
    de chaque route : une route muette y apparaît « (docstring manquante) ». Le
    stock hérité (~96) ne peut que baisser ; toute route NOUVELLE arrive documentée.
    """
    missing = 0
    for path in modules.values():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_route = any(
                isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "route"
                for dec in node.decorator_list
            )
            if is_route and not ast.get_docstring(node):
                missing += 1
    return missing


def count_deep_chains(modules: dict[str, Path]) -> int:
    return sum(len(DEEP_CHAIN_RE.findall(p.read_text(encoding="utf-8"))) for p in modules.values())


def functions_over_limit(modules: dict[str, Path], limit: int = FUNC_LINES_LIMIT) -> list[tuple[str, str, int]]:
    hits: list[tuple[str, str, int]] = []
    for dotted, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = (node.end_lineno or node.lineno) - node.lineno + 1
                if size > limit:
                    hits.append((dotted, node.name, size))
    return sorted(hits, key=lambda h: -h[2])


def collect_metrics(base: Path) -> dict:
    modules = iter_modules(base)
    fanout, deferred, top_edges = build_graph(modules)
    cycles = find_cycles(top_edges)
    init_cycles = find_init_cycles(modules, top_edges)
    return {
        "cycles": len(cycles),
        "cycles_detail": [" -> ".join(c) for c in cycles],
        "init_cycles": len(init_cycles),
        "init_cycles_detail": init_cycles,
        "deferred_internal_imports": sum(deferred.values()),
        "routes_missing_docstring": count_routes_missing_docstring(modules),
        "deep_config_chains": count_deep_chains(modules),
        "functions_over_150": len(functions_over_limit(modules)),
        "fanout": {m: len(s) for m, s in sorted(fanout.items())},
    }


def check_baseline(current: dict, baseline: dict) -> list[str]:
    """Liste des dégradations (vide = ratchet respecté)."""
    problems: list[str] = []
    if current["cycles"]:
        problems.append(f"CYCLES d'imports top-level détectés ({current['cycles']}) : " + "; ".join(current["cycles_detail"]))
    if current.get("init_cycles"):
        problems.append(
            f"CYCLES inter-paquets via __init__ détectés ({current['init_cycles']}) : " + "; ".join(current["init_cycles_detail"])
        )
    for key in ("deferred_internal_imports", "routes_missing_docstring", "deep_config_chains", "functions_over_150"):
        if current[key] > baseline.get(key, 0):
            problems.append(f"{key} : {current[key]} > baseline {baseline.get(key)}")
    base_fanout: dict[str, int] = baseline.get("fanout", {})
    for module, value in current["fanout"].items():
        allowed = base_fanout.get(module, NEW_MODULE_FANOUT_BUDGET)
        if value > allowed:
            origin = "baseline" if module in base_fanout else f"budget nouveau module ({NEW_MODULE_FANOUT_BUDGET})"
            problems.append(f"fan-out de {module} : {value} > {allowed} ({origin})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", type=Path, default=Path("."), help="racine du dépôt")
    parser.add_argument("--json", action="store_true", help="sortie JSON complète")
    parser.add_argument("--write-baseline", metavar="FICHIER", type=Path)
    parser.add_argument("--check-baseline", metavar="FICHIER", type=Path)
    args = parser.parse_args(argv)

    metrics = collect_metrics(args.base)

    if args.write_baseline:
        payload = {k: v for k, v in metrics.items() if k not in ("cycles_detail", "init_cycles_detail")}
        args.write_baseline.write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[audit] baseline écrite : {args.write_baseline}")
        return 0

    if args.check_baseline:
        baseline = json.loads(args.check_baseline.read_text(encoding="utf-8"))
        problems = check_baseline(metrics, baseline)
        if problems:
            print("[audit] RATCHET VIOLÉ — l'architecture s'est dégradée :", file=sys.stderr)
            for p in problems:
                print(f"  ✗ {p}", file=sys.stderr)
            print("(si la dégradation est volontaire et justifiée, re-générer la baseline"
                  " avec --write-baseline dans le même commit, en l'expliquant)", file=sys.stderr)
            return 1
        print("[audit] ratchet OK — aucune dégradation d'architecture.")
        return 0

    if args.json:
        print(json.dumps(metrics, indent=1, sort_keys=True, ensure_ascii=False))
        return 0

    print(f"cycles top-level          : {metrics['cycles']}")
    print(f"cycles inter-paquets init : {metrics['init_cycles']}")
    print(f"imports internes différés : {metrics['deferred_internal_imports']}")
    print(f"routes sans docstring     : {metrics['routes_missing_docstring']}")
    print(f"chaînes config profondes  : {metrics['deep_config_chains']}")
    print(f"fonctions > 150 lignes    : {metrics['functions_over_150']}")
    top = sorted(metrics["fanout"].items(), key=lambda kv: -kv[1])[:10]
    print("fan-out (top 10) :")
    for module, value in top:
        print(f"  {value:3d}  {module}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

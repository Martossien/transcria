#!/usr/bin/env python3
"""Audit du front (vague A3) — budgets templates/JS, ratchet CI.

Métriques (miroir front de scripts/audit_imports.py) :
  - ``inline_js_lines`` : lignes de LOGIQUE JS inline dans les templates —
    exclut les îlots ``<script type="application/json">`` (données pures) et
    les scripts d'initialisation ne contenant QUE des ``window.X = …;``
    (l'exception « init d'une ligne passant des données Jinja » du plan) ;
  - ``template_lines`` / ``js_lines`` : taille par fichier. Un fichier NOUVEAU
    respecte les budgets (template ≤ 400 l., JS ≤ 900 l.) ; un fichier connu de
    la baseline ne peut que baisser.

Usage :
    python scripts/audit_front.py                                  # résumé lisible
    python scripts/audit_front.py --check-baseline FICHIER         # gate CI (exit 1 si dégradation)
    python scripts/audit_front.py --write-baseline FICHIER         # après une vague qui améliore
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "transcria" / "web" / "templates"
STATIC_JS = ROOT / "transcria" / "web" / "static" / "js"

NEW_TEMPLATE_BUDGET = 400
NEW_JS_BUDGET = 900

_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>(.*?)</script>", re.S)
_INIT_LINE = re.compile(r"^\s*window\.[A-Za-z_$][\w$]*\s*=\s*.+;\s*$")


def _inline_logic_lines(html: str) -> int:
    total = 0
    for m in _SCRIPT.finditer(html):
        lines = [line for line in m.group(1).splitlines() if line.strip()]
        if lines and all(_INIT_LINE.match(line) for line in lines):
            continue  # init de données pure (window.X = …) : exception autorisée
        total += len(lines)
    return total


def collect_metrics() -> dict:
    inline_total = 0
    template_lines: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = p.read_text(encoding="utf-8")
        inline_total += _inline_logic_lines(text)
        template_lines[str(p.relative_to(ROOT))] = len(text.splitlines())
    js_lines = {
        str(p.relative_to(ROOT)): len(p.read_text(encoding="utf-8").splitlines())
        for p in sorted(STATIC_JS.rglob("*.js"))
    }
    return {
        "inline_js_lines": inline_total,
        "template_lines": template_lines,
        "js_lines": js_lines,
    }


def check_baseline(current: dict, baseline: dict) -> list[str]:
    problems: list[str] = []
    if current["inline_js_lines"] > baseline.get("inline_js_lines", 0):
        problems.append(
            f"inline_js_lines : {current['inline_js_lines']} > baseline {baseline.get('inline_js_lines')}"
            " (le JS inline ne revient pas — un fichier static/js + init window.X = … d'une ligne)"
        )
    for key, budget, label in (
        ("template_lines", NEW_TEMPLATE_BUDGET, "template"),
        ("js_lines", NEW_JS_BUDGET, "JS"),
    ):
        base = baseline.get(key, {})
        for name, value in current[key].items():
            allowed = base.get(name, budget)
            if value > allowed:
                origin = "baseline" if name in base else f"budget nouveau {label} ({budget})"
                problems.append(f"{label} {name} : {value} lignes > {allowed} ({origin})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-baseline", metavar="FICHIER", type=Path)
    parser.add_argument("--check-baseline", metavar="FICHIER", type=Path)
    args = parser.parse_args(argv)

    metrics = collect_metrics()

    if args.write_baseline:
        args.write_baseline.write_text(
            json.dumps(metrics, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"[audit-front] baseline écrite : {args.write_baseline}")
        return 0

    if args.check_baseline:
        baseline = json.loads(args.check_baseline.read_text(encoding="utf-8"))
        problems = check_baseline(metrics, baseline)
        if problems:
            print("[audit-front] RATCHET VIOLÉ — le front s'est dégradé :")
            for problem in problems:
                print(f"  ✗ {problem}")
            print("(si volontaire et justifié : re-générer avec --write-baseline dans le même commit)")
            return 1
        print("[audit-front] ratchet OK — aucune dégradation du front.")
        return 0

    if args.json:
        print(json.dumps(metrics, indent=1, sort_keys=True, ensure_ascii=False))
        return 0

    biggest_tpl = max(metrics["template_lines"].items(), key=lambda kv: kv[1])
    biggest_js = max(metrics["js_lines"].items(), key=lambda kv: kv[1])
    print(f"JS inline (logique) : {metrics['inline_js_lines']} lignes")
    print(f"Plus gros template  : {biggest_tpl[0]} ({biggest_tpl[1]} l.)")
    print(f"Plus gros JS        : {biggest_js[0]} ({biggest_js[1]} l.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

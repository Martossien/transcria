#!/usr/bin/env python3
"""Configure opencode pour TranscrIA — trouve le binaire et écrit le provider `local`.

opencode doit connaître un provider `local` pointant sur la LLM d'arbitrage
(llama.cpp, OpenAI-compatible). Sans ça, `local/<model>` ne se résout pas et le
résumé/correction échouent silencieusement. Ce script règle ça de façon idempotente.

Exemples :
    venv/bin/python scripts/setup_opencode.py                       # défauts depuis config.yaml
    venv/bin/python scripts/setup_opencode.py --base-url http://192.168.1.59:8080/v1  # nœud distant
    venv/bin/python scripts/setup_opencode.py --model arbitrage
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcria.gpu.opencode_setup import (  # noqa: E402
    default_base_url,
    ensure_agent_permissions,
    ensure_local_provider,
    find_opencode_binary,
)
from transcria.workflow.agent_workspace import resolve_agent_work_root  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=None, help="URL OpenAI de la LLM (défaut : depuis config.yaml)")
    ap.add_argument("--model", default=None, help="nom du modèle servi (défaut : depuis config.yaml)")
    ap.add_argument("--config-path", default="~/.config/opencode/opencode.json",
                    help="chemin du opencode.json (défaut : ~/.config/opencode/opencode.json)")
    ap.add_argument("--context", type=int, default=None,
                    help="limit.context du modèle (défaut : préserve l'existant, sinon 262144)")
    ap.add_argument("--output", type=int, default=None,
                    help="limit.output du modèle (défaut : préserve l'existant, sinon 81920)")
    args = ap.parse_args()

    try:
        from transcria.config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001 — config absente : on tourne sur les défauts
        cfg = {}

    llm = (cfg.get("workflow", {}) or {}).get("arbitration_llm", {}) or {}
    base_url = args.base_url or default_base_url(cfg)
    model = args.model or llm.get("model_id") or "arbitrage"
    if "/" in model:                       # "local/arbitrage" → clé modèle "arbitrage"
        model = model.split("/", 1)[1]

    binary = find_opencode_binary(config_bin=llm.get("opencode_bin"))
    print(f"opencode binaire : {binary or 'INTROUVABLE (installez opencode, cf. docs/INSTALL.md §5)'}")

    path = Path(args.config_path).expanduser()
    data = ensure_local_provider(path, base_url, model, context=args.context, output=args.output)
    limit = data["provider"]["local"]["models"][model].get("limit", {})
    # Politique headless : `external_directory` déterministe (allow sur l'arbre de scratch des
    # agents, deny ailleurs). Sans ça, le défaut opencode `ask` suspend `opencode run` dès
    # qu'un agent (correction/relecture) explore son scratch via glob/grep.
    work_root = resolve_agent_work_root(cfg)
    ensure_agent_permissions(path, work_root)
    print(f"provider 'local' écrit dans {path}")
    print(f"  baseURL = {base_url}")
    print(f"  model   = {model}")
    print(f"  limit   = context {limit.get('context')} / output {limit.get('output')}")
    print(f"  external_directory allow = {work_root}/** (deny ailleurs)")
    print("✅ opencode configuré. Vérifiez la LLM avec scripts/check_arbitrage_llm.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

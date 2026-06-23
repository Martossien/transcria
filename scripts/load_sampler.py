#!/usr/bin/env python3
"""Échantillonneur pour les tests de charge — cf. docs/PLAN_TEST_CHARGE.md.

Poll périodique (CSV sur stdout) de : VRAM/util par GPU (`nvidia-smi`), charge du nœud
(`/capabilities` : inflight/queued/capacity), et batching vLLM (`/metrics` Prometheus :
num_requests_running/waiting, gpu_cache_usage_perc). Tous les endpoints sont optionnels —
un endpoint injoignable est ignoré (colonnes vides), jamais fatal. À lancer en arrière-plan
pendant une campagne `load_test.py`, puis Ctrl-C / kill.

Exemple (split) :
    venv/bin/python scripts/load_sampler.py --interval 2 \
        --capabilities http://localhost:8002/capabilities --api-key split-bench-key \
        --vllm stt=http://localhost:8003/metrics --vllm llm=http://localhost:8080/metrics
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import requests

_VLLM_KEYS = ("vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:gpu_cache_usage_perc")


def _gpu_mem() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        return ";".join(line.replace(", ", "/").strip() for line in out)  # "mem/util" par GPU
    except Exception:
        return ""


def _capabilities(url: str, api_key: str | None) -> str:
    try:
        h = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        d = requests.get(url, headers=h, timeout=5).json()
        load = (d.get("engines") or [{}])[0].get("load") if isinstance(d.get("engines"), list) else d.get("load", {})
        load = load or {}
        return f"{load.get('capacity', '')}/{load.get('inflight', '')}/{load.get('queued', '')}"
    except Exception:
        return ""


def _vllm(url: str) -> str:
    try:
        txt = requests.get(url, timeout=5).text
        vals = {k: "" for k in _VLLM_KEYS}
        for line in txt.splitlines():
            for k in _VLLM_KEYS:
                if line.startswith(k + "{") or line.startswith(k + " "):
                    vals[k] = line.rsplit(" ", 1)[-1]
        return "/".join(vals[k] for k in _VLLM_KEYS)  # running/waiting/kv_usage
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Échantillonneur charge TranscrIA")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--capabilities", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--vllm", action="append", default=[], help="label=url (répétable)")
    args = p.parse_args(argv)

    vllm = []
    for spec in args.vllm:
        label, _, url = spec.partition("=")
        vllm.append((label or url, url))

    header = ["ts", "gpu(mem/util par carte)", "node(cap/inflight/queued)"] + [f"{lbl}(run/wait/kv)" for lbl, _ in vllm]
    print(",".join(header), flush=True)
    try:
        while True:
            row = [f"{time.time():.1f}", _gpu_mem(), _capabilities(args.capabilities, args.api_key or None) if args.capabilities else ""]
            row += [_vllm(url) for _, url in vllm]
            print(",".join(f'"{c}"' for c in row), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

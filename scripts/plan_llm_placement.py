#!/usr/bin/env python3
"""Planifie / vérifie le placement de la LLM d'arbitrage sur les GPU de la machine.

Couche E/S autour de `transcria.gpu.llm_placement` (logique pure, testée) :
  - détecte les GPU (nvidia-smi, repli torch),
  - `plan`   : recommande (ou évalue un palier donné) et, avec --apply, écrit la
               calibration dans config.yaml (ruamel, atomique),
  - `verify` : compare la calibration DÉCLARÉE à la consommation RÉELLE mesurée du
               serveur llama-server (Option A : mesure, ne prédit pas).

Sortie : humaine (défaut), `--format shell` (KEY=VALUE à `eval` depuis install.sh) ou
`--format json`. Codes de retour : 0 = OK/faisable, 2 = non faisable / incohérence,
1 = erreur d'exécution. Conçu pour ne JAMAIS lever d'exception non gérée vers l'appelant.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Permet l'exécution directe (`python scripts/plan_llm_placement.py`) hors PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcria.gpu.llm_placement import (  # noqa: E402
    DEFAULT_DRIFT_PCT,
    DEFAULT_SAFETY_MARGIN_MB,
    Placement,
    evaluate_calibration,
    plan_for_tier,
    recommend,
)


def _run(cmd: list[str], timeout: int = 10) -> str | None:
    """Exécute une commande, renvoie stdout (str) ou None si indisponible/échec/timeout."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def detect_gpu_sizes() -> list[int]:
    """Tailles des GPU (Mio), en ordre physique. nvidia-smi, repli torch, sinon []."""
    out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    if out is not None:
        sizes: list[int] = []
        for line in out.strip().splitlines():
            token = line.strip()
            if token.isdigit():
                sizes.append(int(token))
        if sizes:
            return sizes
    try:
        import torch

        if torch.cuda.is_available():
            return [
                int(torch.cuda.get_device_properties(i).total_memory / (1024 * 1024))
                for i in range(torch.cuda.device_count())
            ]
    except Exception:
        pass
    return []


def _detect_inventory() -> dict[int, dict[str, int | str]]:
    """index physique → {uuid, total_mb, free_mb} (vide si nvidia-smi indisponible)."""
    out = _run(
        ["nvidia-smi", "--query-gpu=index,uuid,memory.total,memory.free", "--format=csv,noheader,nounits"]
    )
    inv: dict[int, dict[str, int | str]] = {}
    if out is None:
        return inv
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            inv[int(parts[0])] = {
                "uuid": parts[1],
                "total_mb": int(parts[2]),
                "free_mb": int(parts[3]),
            }
        except ValueError:
            continue
    return inv


def _pids_on_port(port: int) -> set[int]:
    out = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], timeout=5)
    if not out:
        return set()
    return {int(p) for p in out.split() if p.strip().isdigit()}


def measure_llm_usage(port: int) -> dict[int, int]:
    """VRAM réellement consommée par le serveur LLM, par index physique de GPU (Mio).

    On somme `used_gpu_memory` des processus écoutant sur `port` (PID via lsof), à
    défaut de ceux nommés « llama-server ». Renvoie {} si rien n'est mesurable.
    """
    inv = _detect_inventory()
    uuid_to_index = {str(v["uuid"]): idx for idx, v in inv.items()}
    out = _run(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
         "--format=csv,noheader,nounits"]
    )
    if out is None:
        return {}
    pids = _pids_on_port(port)
    usage: dict[int, int] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        uuid, pid_raw, name, mem_raw = parts[0], parts[1], parts[2].lower(), parts[3]
        try:
            pid = int(pid_raw)
            mem = int(float(mem_raw))
        except ValueError:
            continue
        matched = pid in pids if pids else ("llama-server" in name or "llama_server" in name)
        if not matched:
            continue
        idx = uuid_to_index.get(uuid)
        if idx is None:
            continue
        usage[idx] = usage.get(idx, 0) + mem
    return usage


# ── E/S config (lecture seule ici ; l'écriture passe par gpu_calibration) ───────


def _read_declared(config_path: str) -> tuple[list[int], int, list[int]]:
    """(llm_gpu_indices, llm_vram_mb, llm_vram_mb_per_gpu) depuis config.yaml."""
    import yaml

    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    gpu = cfg.get("gpu", {}) or {}
    indices = [int(i) for i in (gpu.get("llm_gpu_indices") or [])]
    vram_mb = int(gpu.get("llm_vram_mb") or 0)
    per_gpu = [int(x) for x in (gpu.get("llm_vram_mb_per_gpu") or [])]
    return indices, vram_mb, per_gpu


def _read_port(config_path: str, override: int | None) -> int:
    if override:
        return override
    try:
        import yaml

        with open(config_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        services = cfg.get("services", {}) or {}
        return int(services.get("arbitrage_llm_port") or services.get("qwen_port") or 8080)
    except Exception:
        return 8080


# ── Rendu ───────────────────────────────────────────────────────────────────


def _emit_warnings(warnings: list[str]) -> None:
    for w in warnings:
        print(f"  ⚠ {w}", file=sys.stderr)


def _print_placement_human(p: Placement) -> None:
    if p.feasible:
        print(f"✓ Palier {p.tier_gb} Go retenu — {p.reason}")
        print(f"  llm_gpu_indices    : {p.gpu_indices}")
        print(f"  llm_vram_mb        : {p.vram_mb}")
        print(f"  llm_vram_mb_per_gpu: {p.vram_mb_per_gpu}  (estimation split égal)")
        print(f"  ctx                : {p.ctx}")
    else:
        print(f"✗ Non faisable : {p.reason}", file=sys.stderr)
    if p.warnings:
        print("Avertissements :", file=sys.stderr)
        _emit_warnings(p.warnings)


def _print_placement_shell(p: Placement) -> None:
    print(f"LLM_FEASIBLE={1 if p.feasible else 0}")
    print(f"LLM_TIER={p.tier_gb}")
    print(f"LLM_VRAM_MB={p.vram_mb}")
    print(f"LLM_CTX={p.ctx}")
    print(f'LLM_GPU_INDICES="{" ".join(str(i) for i in p.gpu_indices)}"')
    print(f'LLM_VRAM_MB_PER_GPU="{" ".join(str(i) for i in p.vram_mb_per_gpu)}"')
    _emit_warnings(p.warnings)


def _placement_to_dict(p: Placement) -> dict:
    return {
        "tier_gb": p.tier_gb,
        "feasible": p.feasible,
        "reason": p.reason,
        "gpu_indices": p.gpu_indices,
        "vram_mb": p.vram_mb,
        "vram_mb_per_gpu": p.vram_mb_per_gpu,
        "ctx": p.ctx,
        "warnings": p.warnings,
    }


# ── Commandes ─────────────────────────────────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> int:
    sizes = (
        [int(s) for s in args.gpus.replace(",", " ").split()]
        if args.gpus
        else detect_gpu_sizes()
    )
    if not sizes:
        print("✗ Aucun GPU NVIDIA détecté (nvidia-smi/torch indisponibles).", file=sys.stderr)
        return 2

    if args.tier:
        placement = plan_for_tier(args.tier, sizes, safety_margin_mb=args.margin)
    else:
        placement = recommend(sizes, safety_margin_mb=args.margin)

    if args.format == "json":
        print(json.dumps(_placement_to_dict(placement), ensure_ascii=False, indent=2))
    elif args.format == "shell":
        _print_placement_shell(placement)
    else:
        _print_placement_human(placement)

    if args.apply:
        if not placement.feasible:
            print("✗ --apply refusé : placement non faisable.", file=sys.stderr)
            return 2
        if not args.config:
            print("✗ --apply requiert --config.", file=sys.stderr)
            return 1
        try:
            from transcria.config.gpu_calibration import apply_gpu_calibration

            apply_gpu_calibration(
                args.config,
                vram_mb=placement.vram_mb,
                gpu_indices=placement.gpu_indices,
                vram_mb_per_gpu=placement.vram_mb_per_gpu,
            )
            print(f"✓ Calibration écrite dans {args.config}", file=sys.stderr)
        except Exception as exc:
            print(f"✗ Écriture calibration échouée : {exc}", file=sys.stderr)
            return 1

    return 0 if placement.feasible else 2


def cmd_verify(args: argparse.Namespace) -> int:
    if not Path(args.config).is_file():
        print(f"✗ config introuvable : {args.config}", file=sys.stderr)
        return 1
    indices, vram_mb, per_gpu = _read_declared(args.config)
    if not indices:
        print("✗ gpu.llm_gpu_indices absent de la config — rien à vérifier.", file=sys.stderr)
        return 1

    if os.environ.get("CUDA_VISIBLE_DEVICES"):
        print(
            "  ⚠ CUDA_VISIBLE_DEVICES est défini dans cet environnement : la mesure "
            "raisonne en index physiques, vérifiez la correspondance.",
            file=sys.stderr,
        )

    port = _read_port(args.config, args.port)
    inv = _detect_inventory()
    if not inv:
        print("✗ nvidia-smi indisponible — mesure impossible.", file=sys.stderr)
        return 1
    observed = measure_llm_usage(port)
    if not observed:
        print(
            f"✗ Aucune VRAM LLM mesurée sur le port {port} : le serveur llama-server "
            "tourne-t-il ? (lancez-le, puis relancez ce contrôle)",
            file=sys.stderr,
        )
        return 2

    total_map = {idx: int(v["total_mb"]) for idx, v in inv.items()}
    free_map = {idx: int(v["free_mb"]) for idx, v in inv.items()}

    report = evaluate_calibration(
        declared_indices=indices,
        declared_vram_mb=vram_mb,
        declared_per_gpu=per_gpu or None,
        observed_per_gpu=observed,
        free_per_gpu=free_map,
        total_per_gpu=total_map,
        safety_margin_mb=args.margin,
        drift_pct=args.drift,
    )

    if args.format == "json":
        print(json.dumps({
            "ok": report.ok,
            "per_gpu": [vars(g) for g in report.per_gpu],
            "warnings": report.warnings,
            "suggested_vram_mb_per_gpu": report.suggested_vram_mb_per_gpu,
            "suggested_vram_mb": report.suggested_vram_mb,
        }, ensure_ascii=False, indent=2))
        return 0 if report.ok else 2

    print("━━━ Calibration LLM : déclaré vs mesuré ━━━")
    for g in report.per_gpu:
        mark = {"ok": "✓", "warn": "⚠", "critical": "🔴"}.get(g.level, "?")
        print(
            f"  {mark} GPU {g.index} : déclaré {g.declared_mb} Mio · observé {g.observed_mb} "
            f"Mio · libre {g.free_mb}/{g.total_mb} Mio — {g.note}"
        )
    if report.ok:
        print("✓ Calibration conforme à la réalité mesurée.")
    else:
        print("⚠ Incohérences détectées :", file=sys.stderr)
        _emit_warnings(report.warnings)
        print(
            f"\n  Calibration mesurée à appliquer dans config.yaml (gpu:) :\n"
            f"    llm_vram_mb: {report.suggested_vram_mb}\n"
            f"    llm_vram_mb_per_gpu: {report.suggested_vram_mb_per_gpu}",
            file=sys.stderr,
        )
    return 0 if report.ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="recommander/évaluer un placement (et l'appliquer)")
    p.add_argument("--gpus", help="tailles en Mio, séparées par des virgules (sinon auto-détection)")
    p.add_argument("--tier", type=int, help="forcer un palier (12/16/24/32/48/64) au lieu de recommander")
    p.add_argument("--config", help="chemin config.yaml (requis avec --apply)")
    p.add_argument("--apply", action="store_true", help="écrire la calibration dans --config")
    p.add_argument("--margin", type=int, default=DEFAULT_SAFETY_MARGIN_MB, help="marge libre/carte (Mio)")
    p.add_argument("--format", choices=("human", "shell", "json"), default="human")
    p.set_defaults(func=cmd_plan)

    v = sub.add_parser("verify", help="comparer la calibration déclarée à la consommation réelle")
    v.add_argument("--config", default="./config.yaml")
    v.add_argument("--port", type=int, help="port du serveur LLM (sinon lu dans la config)")
    v.add_argument("--margin", type=int, default=DEFAULT_SAFETY_MARGIN_MB, help="marge libre/carte (Mio)")
    v.add_argument("--drift", type=int, default=DEFAULT_DRIFT_PCT, help="seuil de dérive (%%)")
    v.add_argument("--format", choices=("human", "json"), default="human")
    v.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # pragma: no cover - filet de sécurité ultime
        print(f"✗ Erreur inattendue : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

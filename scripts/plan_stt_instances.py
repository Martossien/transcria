#!/usr/bin/env python3
"""Plan multi-instance STT en ligne de commande (pendant CLI de la page admin).

Même moteur que /admin/hardware (planificateur pur + écriture ruamel ciblée) —
pour les installations sans UI sous la main (nœud de ressources, scripts).

Usage :
  venv/bin/python scripts/plan_stt_instances.py plan  [--config config.yaml] [--format human|json]
  venv/bin/python scripts/plan_stt_instances.py plan  --config config.yaml --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_config(path: str) -> dict:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def cmd_plan(args: argparse.Namespace) -> int:
    from transcria.gpu.hardware_advisor import _detect_gpu_totals_mb, stt_instances_card

    config = _load_config(args.config)
    totals = _detect_gpu_totals_mb()
    if not totals:
        print("Aucun GPU détecté (nvidia-smi indisponible).", file=sys.stderr)
        return 2
    card = stt_instances_card(config, totals)
    if card is None:
        print("Aucun backend STT servi configuré (inference.stt.backends.<nom>.url).",
              file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps({
            "status": card.status, "current": card.current,
            "recommended": card.recommended, "detail": card.detail,
            "applicable": card.applicable, "plan": card.apply_payload or None,
            "gpu_totals_mb": totals,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"GPU détectés : {', '.join(f'#{i} {t} Mo' for i, t in sorted(totals.items()))}")
        print(f"Actuellement  : {card.current}")
        print(f"Préconisation : {card.recommended}")
        print(f"Détail        : {card.detail}")

    if not args.apply:
        return 0 if card.status != "info" else 2
    if not card.applicable:
        print("Rien à appliquer (déjà au niveau du matériel, ou infaisable).", file=sys.stderr)
        return 2
    from transcria.config.stt_instances_config import apply_stt_instances

    apply_stt_instances(args.config, **card.apply_payload)
    print(f"Plan appliqué dans {args.config} — redémarrer le service pour l'appliquer.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="calcule (et applique avec --apply) le plan multi-instance")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.add_argument("--apply", action="store_true",
                   help="écrit le plan dans la config (ruamel ciblé, atomique)")
    p.set_defaults(func=cmd_plan)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

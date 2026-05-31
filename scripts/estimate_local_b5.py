#!/usr/bin/env python3
"""Génère des estimations locales B5 depuis les résultats de bench existants."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from transcria.benchmarks.stt_concurrency_estimator import collect_measurements
from transcria.benchmarks.stt_concurrency_estimator import estimate_local_concurrency
from transcria.benchmarks.stt_concurrency_estimator import write_estimates


BENCH_RESULTS_DIR = REPO_ROOT / "bench_results"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estime l'impact de la concurrence STT distante à partir des logs/JSON "
            "de bench locaux. Les résultats valent uniquement pour cette machine."
        )
    )
    parser.add_argument("--bench-root", type=Path, default=BENCH_RESULTS_DIR, help="Racine des bench_results locaux.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Répertoire de sortie (défaut: bench_results/local_b5_estimates_<timestamp>).",
    )
    parser.add_argument("--workers", default="2,4,8", help="Workers STT à estimer, ex: 2,4,8.")
    parser.add_argument(
        "--efficiency",
        type=float,
        default=0.75,
        help="Efficacité marginale d'un worker supplémentaire (0<valeur<=1, défaut: 0.75).",
    )
    parser.add_argument("--include-failed", action="store_true", help="Inclure les runs marqués en échec.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Logs détaillés.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s - %(message)s",
    )
    workers = _parse_workers(args.workers)
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = BENCH_RESULTS_DIR / f"local_b5_estimates_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    measurements = collect_measurements(args.bench_root, include_failed=args.include_failed)
    if not measurements:
        logging.error("Aucune mesure exploitable trouvée dans %s", args.bench_root)
        return 1

    estimates = estimate_local_concurrency(measurements, target_workers=workers, efficiency=args.efficiency)
    csv_path, md_path = write_estimates(estimates, output_dir)
    logging.info("Mesures sources : %d", len(measurements))
    logging.info("Estimations : %d", len(estimates))
    logging.info("CSV : %s", csv_path)
    logging.info("Markdown : %s", md_path)
    logging.info("Portée : machine_locale ; source : estimation, pas mesure de serveur distant")
    return 0


def _parse_workers(raw: str) -> list[int]:
    workers: list[int] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        workers.append(int(value))
    if not workers:
        raise ValueError("--workers doit contenir au moins un entier")
    return workers


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Générateur de charge concurrente pour TranscrIA — cf. docs/PLAN_TEST_CHARGE.md.

Lance N jobs en RAFALE (tous démarrés ~simultanément, pire cas d'admission), chacun via le
flux E2E complet de `verify_split_topology.run_job` (login → upload → wizard → process →
poll → download des livrables). `run_job` **lève** au moindre échec (y compris livrable vide)
⇒ un job qui se termine sans exception = succès P0 avec livrables valides.

Sortie : récap agrégé (succès/total, débit, latences p50/p95) + code 0 si 100 % de succès,
non-zéro sinon. Chaque échec est reporté avec sa raison (stderr de `run_job`).

Exemples :
    # all-in-one : 10 jobs en rafale
    venv/bin/python scripts/load_test.py --web http://localhost:7870 \
        --jobs 10 --audio tests/test2.mp3 --username admin --password "$PWD_ADMIN"

    # split : 8 jobs en rafale
    venv/bin/python scripts/load_test.py --web http://localhost:7870 \
        --jobs 8 --audio tests/test2.mp3 --username admin --password "$PWD_ADMIN"
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_split_topology as vst  # noqa: E402

_print_lock = threading.Lock()


def _prefixed_log(stage: str, msg: str) -> None:
    """`_log` thread-safe préfixé par le job courant (évite l'entrelacement illisible)."""
    with _print_lock:
        print(f"[{threading.current_thread().name}|{stage}] {msg}", flush=True)


@dataclass
class JobResult:
    index: int
    ok: bool
    elapsed_s: float
    error: str = ""


def _run_one(index: int, args, barrier: threading.Barrier, results: list[JobResult]) -> None:
    barrier.wait()  # libère tous les threads en même temps → vraie rafale
    t0 = time.monotonic()
    try:
        vst.run_job(
            web_url=args.web.rstrip("/"),
            audio=args.audio,
            username=args.username,
            password=args.password,
            mode=args.mode,
            timeout_s=args.timeout,
            poll_s=args.poll_interval,
        )
        results[index] = JobResult(index, True, time.monotonic() - t0)
    except BaseException as exc:  # SystemExit (via _fail) inclus
        results[index] = JobResult(index, False, time.monotonic() - t0, repr(exc))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Générateur de charge concurrente TranscrIA")
    p.add_argument("--web", default="http://localhost:7870")
    p.add_argument("--jobs", type=int, required=True, help="nombre de jobs lancés en rafale")
    p.add_argument("--audio", type=Path, default=Path("tests/test2.mp3"))
    p.add_argument("--username", default="admin")
    p.add_argument("--password", required=True)
    p.add_argument("--mode", default="quality", choices=("fast", "quality"))
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--poll-interval", type=float, default=5.0)
    p.add_argument("--label", default="load", help="préfixe de nom de thread")
    args = p.parse_args(argv)

    if not args.audio.is_file():
        print(f"audio introuvable : {args.audio}", file=sys.stderr)
        return 2

    # run_job logge via vst._log ; on le rend thread-safe + préfixé.
    vst._log = _prefixed_log

    results: list[JobResult] = [JobResult(i, False, 0.0, "non démarré") for i in range(args.jobs)]
    barrier = threading.Barrier(args.jobs)
    threads = [
        threading.Thread(target=_run_one, args=(i, args, barrier, results), name=f"{args.label}{i:02d}")
        for i in range(args.jobs)
    ]

    print(f"=== CHARGE : {args.jobs} jobs en rafale → {args.web} (audio={args.audio.name}, mode={args.mode}) ===", flush=True)
    wall0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - wall0

    ok = [r for r in results if r.ok]
    ko = [r for r in results if not r.ok]
    lat = sorted(r.elapsed_s for r in ok)

    print("\n=== RÉCAP ===", flush=True)
    print(f"jobs            : {len(results)}")
    print(f"succès (P0)     : {len(ok)}/{len(results)}")
    print(f"échecs          : {len(ko)}")
    print(f"temps mur total : {wall:.1f}s")
    if ok:
        print(f"débit           : {len(ok) / wall * 60:.2f} jobs/min")
        print(f"latence min/p50/p95/max : "
              f"{lat[0]:.1f} / {statistics.median(lat):.1f} / "
              f"{lat[min(len(lat) - 1, int(0.95 * len(lat)))]:.1f} / {lat[-1]:.1f} s")
    for r in ko:
        print(f"  ÉCHEC job#{r.index} après {r.elapsed_s:.1f}s : {r.error}", file=sys.stderr)

    # P0 : succès = 100 %.
    return 0 if not ko else 1


if __name__ == "__main__":
    sys.exit(main())

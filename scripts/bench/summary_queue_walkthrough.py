#!/usr/bin/env python3
"""Banc Playwright du bouton « Générer le résumé » — GESTE réel, sans GPU ni LLM.

Depuis le 2026-11-25, `POST /api/jobs/<id>/summary` ne tient plus la requête HTTP pendant la
phase : il ENFILE le résumé et répond aussitôt. Ce banc le prouve à l'écran sur une instance
JETABLE (config scratch, SQLite, file activée mais mise en PAUSE par une fenêtre calendaire
couvrant toute la semaine) : la réponse arrive en moins de 5 s, le bandeau « en file » s'affiche
sans erreur réseau, un second clic ne ré-enfile pas, l'état `queued` est visible via GET /status,
et la page rechargée montre l'attente au lieu du bouton.

    S=/tmp/sum_bench ; mkdir -p $S
    venv/bin/python scripts/bench/summary_queue_walkthrough.py seed --config $S/config.yaml --db $S/app.db
    venv/bin/python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
        --server "TRANSCRIA_CONFIG=$S/config.yaml TRANSCRIA_DATABASE_URL=sqlite:///$S/app.db TRANSCRIA_SECRET=bench \\
                  venv/bin/python app.py --port 7899" --port 7899 \\
        -- venv/bin/python scripts/bench/summary_queue_walkthrough.py walk --base-url http://localhost:7899 \\
           --scratch $S --out $S/shots

Échoue (exit ≠ 0) sur toute erreur serveur, assertion manquée ou erreur console JS. Les captures
sont ensuite RELUES par un humain (règle du projet : piloter ET voir).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ADMIN_PW = "sum-bench-admin-pw"
ALL_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def cmd_seed(args) -> int:
    scratch = Path(args.config).resolve().parent
    (scratch / "jobs").mkdir(parents=True, exist_ok=True)
    Path(args.config).write_text(f"""
runtime: {{role: all}}
auth: {{first_admin_username: admin, first_admin_password: {ADMIN_PW}}}
storage: {{jobs_dir: {scratch / 'jobs'}, shared_backend: fs}}
notifications: {{email: {{enabled: false}}}}
workflow:
  queue: {{enabled: true}}
  scheduling: {{enabled: true, timezone: Europe/Paris}}
  summary_autostart: {{enabled: false}}
""", encoding="utf-8")
    os.environ["TRANSCRIA_CONFIG"] = str(args.config)
    os.environ["TRANSCRIA_DATABASE_URL"] = f"sqlite:///{args.db}"
    os.environ.setdefault("TRANSCRIA_SECRET", "bench")
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from app import create_app

    app = create_app(str(args.config), start_background_services=False)
    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.queue.calendar import SchedulingWindowStore

        admin = UserStore.get_by_username("admin")
        assert admin is not None, "admin non créé"
        # File en PAUSE toute la semaine : l'entrée reste « waiting », rien ne tourne (pas de GPU).
        SchedulingWindowStore.create({"name": "banc : file en pause", "days": ALL_DAYS,
                                      "start": "00:00", "end": "23:59", "action": "pause_queue"})
        job = JobStore.create_job(admin.id, title="Banc résumé en file")
        fs = JobFilesystem(str(scratch / "jobs"), job.id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        shutil.copy(root / "tests" / "test1.mp3", fs.job_dir / "input" / "original.mp3")
        fs.save_json("metadata/audio_analysis.json", {"duration_seconds": 18.0, "sample_rate": 44100})
        JobStore.update_state(job.id, JobState.ANALYZED)
        (scratch / "job_id").write_text(job.id, encoding="utf-8")
    print(f"seed OK → {args.config} (job {job.id})")
    return 0


def cmd_walk(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    job_id = (Path(args.scratch) / "job_id").read_text(encoding="utf-8").strip()
    failures: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    summary_calls: list[tuple[int, float]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("[OK]   " if ok else "[FAIL] ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("response", lambda r: server_errors.append(f"{r.status} {r.url}") if r.status >= 500 else None)
        page.on("dialog", lambda d: d.accept())

        started: dict[str, float] = {}
        page.on("request", lambda rq: started.__setitem__(rq.url, time.monotonic())
                if rq.url.endswith(f"/api/jobs/{job_id}/summary") else None)
        page.on("response", lambda r: summary_calls.append((r.status, time.monotonic() - started.get(r.url, time.monotonic())))
                if r.url.endswith(f"/api/jobs/{job_id}/summary") else None)

        def shot(name: str) -> None:
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)

        page.goto(f"{args.base_url}/login", wait_until="networkidle")
        page.fill("input[name=username]", args.user)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{args.base_url}/jobs/{job_id}", wait_until="networkidle")
        btn = page.locator("[data-action='TranscrIA.generateSummary']")
        check("étape 3 : bouton « Générer le résumé » présent", btn.count() == 1)
        shot("01_avant_clic")

        # 1. Le clic : réponse immédiate, bandeau « en file », aucune erreur réseau.
        btn.first.click()
        banner = page.locator("#summary-result .alert")
        banner.wait_for(timeout=15000)
        text = banner.text_content() or ""
        check("réponse du POST /summary en < 5 s",
              bool(summary_calls) and summary_calls[0][0] == 200 and summary_calls[0][1] < 5.0,
              f"{summary_calls}")
        check("bandeau « en file » affiché", "en file" in text and "arrière-plan" in text, text[:120])
        check("aucune « Erreur réseau »", "Erreur réseau" not in text)
        shot("02_apres_clic_en_file")

        # 2. L'état serveur : entrée de file en attente, pas d'exécution dans la requête.
        status = page.evaluate(f"fetch('/api/jobs/{job_id}/status').then(r => r.json())")
        check("GET /status : execution_status = queued", status.get("execution_status") == "queued", str(status.get("execution_status")))
        check("GET /status : état du job inchangé (analyzed)", status.get("state") == "analyzed", str(status.get("state")))
        check("GET /status : position dans la file", bool(status.get("queue_position")), str(status.get("queue_position")))

        # 3. Second clic (double clic, F5 impatient…) : « déjà en file », pas de second enfilage.
        second = page.evaluate(
            f"fetch('/api/jobs/{job_id}/summary', {{method: 'POST'}}).then(r => r.json().then(b => [r.status, b]))")
        check("second POST : 200 queued sans ré-enfiler", second[0] == 200 and second[1].get("queued") is True, str(second))

        # 4. La page rechargée dit l'attente (bandeau de statut), plus de bouton actif.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        body = page.locator("body").inner_text()
        check("page rechargée : l'attente en file est dite",
              ("file" in body.lower() and ("attente" in body.lower() or "position" in body.lower())), "")
        gen = page.locator("[data-action='TranscrIA.generateSummary']")
        check("page rechargée : bouton absent ou désactivé",
              gen.count() == 0 or gen.first.is_disabled() or not gen.first.is_visible())
        shot("03_rechargee_en_attente")

        browser.close()

    check("aucune erreur serveur (5xx)", not server_errors, "; ".join(server_errors[:3]))
    check("aucune erreur console JS", not console_errors, "; ".join(console_errors[:3]))
    print(f"\n{len(failures)} échec(s) — captures dans {out}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--config", required=True)
    s.add_argument("--db", required=True)
    w = sub.add_parser("walk")
    w.add_argument("--base-url", default="http://localhost:7899")
    w.add_argument("--scratch", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--user", default="admin")
    w.add_argument("--password", default=ADMIN_PW)
    args = ap.parse_args(argv)
    return cmd_seed(args) if args.cmd == "seed" else cmd_walk(args)


if __name__ == "__main__":
    sys.exit(main())

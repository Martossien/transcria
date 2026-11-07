#!/usr/bin/env python3
"""Banc Playwright de « Discuter avec mes réunions » et « Mes actions » — GESTES réels, sans GPU.

Trois sous-commandes, sur une instance JETABLE (config scratch, SQLite, jobs factices, LLM FACTICE
compatible OpenAI) — jamais le service de production :

    fake-llm  --port 7903 --host 127.0.0.2     # /v1/models + /v1/chat/completions, réponses canoniques
    seed      --config <scratch>/config.yaml    # admin + trois réunions (dont une confidentielle, une avec SRT)
    walk      --base-url http://localhost:7899 --user admin --password <pw> --out <dossier>

Enchaînement type (le helper with_server attend que les ports répondent) :

    S=/tmp/ask_bench ; mkdir -p $S/jobs
    venv/bin/python scripts/bench/ask_meetings_walkthrough.py seed --config $S/config.yaml --db $S/app.db
    venv/bin/python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
        --server "venv/bin/python scripts/bench/ask_meetings_walkthrough.py fake-llm --port 7903" --port 7903 \\
        --server "TRANSCRIA_CONFIG=$S/config.yaml TRANSCRIA_DATABASE_URL=sqlite:///$S/app.db TRANSCRIA_SECRET=bench \\
                  venv/bin/python app.py --port 7899" --port 7899 \\
        -- venv/bin/python scripts/bench/ask_meetings_walkthrough.py walk --base-url http://localhost:7899 \\
           --user admin --password ask-bench-admin-pw --out $S/shots

Échoue (exit ≠ 0) sur toute erreur serveur, assertion clé manquée ou erreur console JS. Les captures
sont ensuite RELUES par un humain (règle du projet : piloter ET voir).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ADMIN_PW = "ask-bench-admin-pw"


# ── LLM factice ──────────────────────────────────────────────────────────────

class _FakeLLM(BaseHTTPRequestHandler):
    """Comprend le contrat du prompt : cite [1] quand il y a des réunions, « non retrouvé » si la
    question contient « introuvable » au niveau fiches (pour exercer l'escalade)."""

    def log_message(self, *args) -> None:  # silence
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/v1/models"):
            return self._json({"data": [{"id": "arbitrage"}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if self.path.rstrip("/").endswith("/v1/completions"):
            return self._json({"choices": [{"text": "ok", "reasoning_content": ""}]})
        messages = payload.get("messages") or []
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        question = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        numbered = re.findall(r"^\[(\d+)\] (.+)$", system, flags=re.M)
        if "lentement" in question.lower():
            import time
            time.sleep(6)                      # laisse le temps de quitter la page et d'y revenir
        cards_only = "fiches seules" in system or "cards only" in system
        if "### Transcription [" in system or "### Transcript [" in system:
            answer = "D'après la transcription, la phrase exacte est « le devis part vendredi » [1]."
        elif not numbered or (cards_only and "introuvable" in question.lower()):
            answer = "Non retrouvé dans les réunions consultées."
        else:
            first = numbered[0][1]
            answer = (f"Le budget de formation a été validé lors de « {first} » [1] ; "
                      f"une action de suivi est attendue [1]. Hors liste : [9].")
        return self._json({"choices": [{"message": {"role": "assistant", "content": answer}}]})


def cmd_fake_llm(args) -> int:
    """Deux écoutes locales : 127.0.0.1 (sonde de disponibilité du helper) et ``--host``
    (127.0.0.2 par défaut : l'application doit voir la LLM comme DISTANTE — verrou no-op,
    aucun lancement local — ce qu'un hôte 127.0.0.1 n'obtiendrait pas)."""
    import threading

    servers = [ThreadingHTTPServer((host, args.port), _FakeLLM) for host in dict.fromkeys(("127.0.0.1", args.host))]
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"LLM factice sur http://{args.host}:{args.port}/v1", flush=True)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ── Semis ────────────────────────────────────────────────────────────────────

def cmd_seed(args) -> int:
    import os

    scratch = Path(args.config).parent
    jobs_dir = scratch / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (scratch / "agent_work").mkdir(exist_ok=True)
    Path(args.config).write_text(f"""
runtime: {{role: all}}
auth: {{first_admin_username: admin, first_admin_password: {ADMIN_PW}}}
storage: {{jobs_dir: {jobs_dir}, agent_work_dir: {scratch / 'agent_work'}, shared_backend: fs}}
notifications: {{email: {{enabled: false}}}}
services: {{backend: http, arbitrage_llm_host: {args.llm_host}, arbitrage_llm_port: {args.llm_port}, arbitrage_api_model_id: arbitrage}}
workflow:
  arbitration_llm: {{enabled: true, model_id: local/arbitrage, api_base: "http://{args.llm_host}:{args.llm_port}/v1"}}
  queue: {{enabled: true, poll_interval_s: 1}}
  ask_meetings: {{enabled: true, llm_lock_timeout_s: 5, level2_meetings: 2}}
""", encoding="utf-8")
    os.environ["TRANSCRIA_CONFIG"] = str(args.config)
    os.environ["TRANSCRIA_DATABASE_URL"] = f"sqlite:///{args.db}"
    os.environ.setdefault("TRANSCRIA_SECRET", "bench")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app import create_app
    from transcria.auth.store import UserStore
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore

    app = create_app(str(args.config), start_background_services=False)
    with app.app_context():
        admin = UserStore.get_by_username("admin")
        assert admin is not None, "admin non créé"
        meetings = [
            ("Comité budget formation 2026", "Réunion interne", "normal", "2026-10-03",
             "Validation du budget de formation et choix du prestataire.",
             ["Budget formation 2026 validé", "Prestataire Organisation A retenu"],
             ["Personne A : envoyer le devis signé (vendredi)", "Personne B : planifier la session de rentrée (fin octobre)"], True),
            ("Point projet Zabéo", "Réunion projet", "normal", "2026-11-02",
             "Avancement du projet, points bloquants, prochaine livraison.",
             ["Livraison décalée au 20 novembre"],
             ["Personne B : mettre à jour le planning", "Rédiger le compte rendu pour le comité"], False),
            ("Entretien annuel", "Réunion interne", "high", "2026-09-15",
             "Entretien individuel confidentiel.", [], ["Personne C : préparer le plan de formation"], False),
        ]
        for title, mtype, sens, when, topic, decisions, actions, with_srt in meetings:
            job = JobStore.create_job(admin.id, title)
            JobStore.update_state(job.id, JobState.COMPLETED)
            fs = JobFilesystem(str(jobs_dir), job.id)
            fs.save_json("context/meeting_context.json", {
                "title": title, "meeting_type": mtype, "sensitivity": sens, "date": when, "language": "fr",
                "topic": topic, "objective": "Décider et répartir les actions.", "notes": "1. Budget 2. Prestataire 3. Actions",
                "summary": f"Synthèse de la réunion « {title} » : {topic} Les participants ont validé les points à l'ordre du jour.",
                "structured_data": {"decisions": decisions, "actions": actions, "prochaine_date": ""},
            })
            fs.save_json("context/participants.json", [
                {"id": "p1", "name": "Personne A", "function": "Fonction A"},
                {"id": "p2", "name": "Personne B", "function": "Fonction B"}])
            if with_srt:
                fs.save_text("metadata/transcription_corrigee.srt",
                             "1\n00:00:00,000 --> 00:00:04,000\nSPEAKER_00(Personne A): le devis part vendredi\n\n"
                             "2\n00:00:04,000 --> 00:00:08,000\nSPEAKER_01(Personne B): très bien, je planifie la rentrée\n")
        from transcria.job_cards.store import JobCardStore
        counts = JobCardStore.rebuild_all(str(jobs_dir))
        print("semis :", counts)
    return 0


# ── Parcours ─────────────────────────────────────────────────────────────────

def cmd_walk(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("[OK]   " if ok else "[FAIL] ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("response", lambda r: server_errors.append(f"{r.status} {r.url}") if r.status >= 500 else None)

        def shot(name: str) -> None:
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)

        page.goto(f"{args.base_url}/login", wait_until="networkidle")
        page.fill("input[name=username]", args.user)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        # 1. Page Discuter, vide
        page.goto(f"{args.base_url}/meetings/ask", wait_until="networkidle")
        check("page discuter rendue", page.locator("#ask-form").count() == 1)
        check("entrée de menu active", page.locator("a.nav-link.active[href='/meetings/ask']").count() == 1)
        shot("01_discuter_vide")

        # 2. Question → attente → réponse citée
        page.fill("#ask-question", "Qu'a-t-on décidé sur le budget de formation ?")
        page.click("#ask-submit")
        page.wait_for_selector("[data-ask-pending]", timeout=5000)
        shot("02_discuter_en_attente")
        page.wait_for_selector("[data-ask-turn=assistant]", timeout=60000)
        answer = page.locator("[data-ask-turn=assistant] .ask-answer").first.inner_text()
        check("réponse citée [1]", "[1]" in answer and "[9]" not in answer, answer[:80])
        check("carte de la réunion citée", page.locator("[data-ask-cited]").count() == 1)
        check("niveau affiché", "fiches" in page.locator("[data-ask-turn=assistant] .small").first.inner_text())
        shot("03_discuter_reponse")

        # 3. Niveau 3 depuis la carte
        page.click("[data-ask-cited] button")
        check("cible annoncée", page.locator("#ask-target").is_visible())
        page.fill("#ask-question", "Quelle phrase exacte a été dite sur le devis ?")
        page.click("#ask-submit")
        page.wait_for_selector("[data-ask-turn=assistant] >> nth=1", timeout=60000)
        second = page.locator("[data-ask-turn=assistant]").nth(1).inner_text()
        check("réponse de niveau transcription", "transcription" in second.lower() and "devis part vendredi" in second)
        check("cible effacée après envoi", not page.locator("#ask-target").is_visible())
        shot("04_discuter_transcription")

        # 4. Escalade puis non retrouvé (question « introuvable » : la LLM factice ne cite rien)
        page.fill("#ask-question", "Sujet introuvable ?")
        page.click("#ask-submit")
        page.wait_for_selector("[data-ask-turn=assistant] >> nth=2", timeout=60000)
        third = page.locator("[data-ask-turn=assistant]").nth(2).inner_text()
        check("non retrouvé dit clairement", "Aucune des réunions" in third, third[:80])
        shot("05_discuter_non_retrouve")

        # 5. Rechargement : l'historique revient
        page.reload(wait_until="networkidle")
        page.wait_for_selector("[data-ask-turn=assistant]", timeout=10000)
        check("historique rechargé", page.locator("[data-ask-turn=user]").count() == 3)
        shot("06_discuter_historique_recharge")

        # 6. Parti avant la réponse : retour sur la page, la demande en cours est reprise
        page.fill("#ask-question", "Réponds lentement : le budget ?")
        page.click("#ask-submit")
        page.wait_for_selector("[data-ask-pending]", timeout=5000)
        page.goto(f"{args.base_url}/meetings/actions", wait_until="networkidle")
        page.goto(f"{args.base_url}/meetings/ask", wait_until="networkidle")
        page.wait_for_selector("[data-ask-pending]", timeout=5000)
        check("demande en cours reprise au retour", page.locator("[data-ask-turn=user]").count() == 4)
        page.wait_for_timeout(2500)                      # une interrogation de plus : l'étape du worker arrive
        pending_text = page.locator("[data-ask-pending]").inner_text()
        check("étape « pourquoi j'attends » affichée", "rédaction" in pending_text or "occupée" in pending_text, pending_text)
        shot("06b_discuter_reprise_demande_en_cours")
        page.wait_for_selector("[data-ask-turn=assistant] >> nth=3", timeout=60000)
        check("réponse arrivée après le retour", page.locator("[data-ask-pending]").count() == 0)

        # 7. Nouvelle conversation, puis retour à l'ancienne par le sélecteur
        page.click("#ask-new")
        check("fil vidé", page.locator("[data-ask-turn]").count() == 0)
        options = page.locator("#ask-conversations option")
        check("conversation récente listée", options.count() >= 2)
        page.select_option("#ask-conversations", index=1)
        page.wait_for_selector("[data-ask-turn=assistant]", timeout=10000)
        check("ancienne conversation rechargée", page.locator("[data-ask-turn=user]").count() == 4)
        shot("06c_discuter_selecteur_conversations")
        page.click("#ask-new")

        # 8. Mes actions
        page.goto(f"{args.base_url}/meetings/actions", wait_until="networkidle")
        html = page.content()
        check("actions par réunion", page.locator("[data-actions-group]").count() == 2 and "exclue" in html)
        check("confidentielle exclue", "plan de formation" not in html)
        shot("07_actions_par_reunion")
        page.select_option("#af-by", "owner")
        page.click("form[data-actions-filters] button[type=submit]")
        page.wait_for_load_state("networkidle")
        check("actions par responsable", page.locator("[data-actions-group]").count() == 3)   # A, B, non attribué
        shot("08_actions_par_responsable")
        page.check("#af-conf")
        page.click("form[data-actions-filters] button[type=submit]")
        page.wait_for_load_state("networkidle")
        check("confidentielle incluse sur demande", "plan de formation" in page.content())
        shot("09_actions_confidentielles_incluses")

        browser.close()

    check("aucune erreur console", not console_errors, "; ".join(console_errors)[:200])
    check("aucune erreur serveur", not server_errors, "; ".join(server_errors)[:200])
    print(f"captures : {out}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fake-llm")
    f.add_argument("--host", default="127.0.0.2")
    f.add_argument("--port", type=int, default=7903)
    f.set_defaults(func=cmd_fake_llm)
    s = sub.add_parser("seed")
    s.add_argument("--config", required=True)
    s.add_argument("--db", required=True)
    s.add_argument("--llm-host", default="127.0.0.2")
    s.add_argument("--llm-port", type=int, default=7903)
    s.set_defaults(func=cmd_seed)
    w = sub.add_parser("walk")
    w.add_argument("--base-url", required=True)
    w.add_argument("--user", default="admin")
    w.add_argument("--password", default=ADMIN_PW)
    w.add_argument("--out", required=True)
    w.set_defaults(func=cmd_walk)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

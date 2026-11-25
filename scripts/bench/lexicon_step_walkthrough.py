#!/usr/bin/env python3
"""Banc Playwright de l'étape 6 « Lexique de session » — GESTES réels, sans GPU ni LLM.

Instance JETABLE (config scratch, SQLite). Un job est posé à l'étape 6 avec une transcription
rapide et un lexique de session façonnés comme un cas réel (2026-11-12, noms neutres) : une fiche
à l'envers (« mot suspect » sans variante), une fiche retournée de travers (variante = bonne
forme), une forme-phrase, une variante qui est la forme validée d'une autre ligne, et des fiches
saines. Vérifie : l'aperçu du sens avec compteurs, les avertissements, le bouton d'échange, la
sauvegarde puis la relecture après rechargement.

    S=/tmp/lex_bench ; mkdir -p $S
    venv/bin/python scripts/bench/lexicon_step_walkthrough.py seed --config $S/config.yaml --db $S/app.db
    venv/bin/python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
        --server "TRANSCRIA_CONFIG=$S/config.yaml TRANSCRIA_DATABASE_URL=sqlite:///$S/app.db TRANSCRIA_SECRET=bench \\
                  venv/bin/python app.py --port 7899" --port 7899 \\
        -- venv/bin/python scripts/bench/lexicon_step_walkthrough.py walk --base-url http://localhost:7899 \\
           --scratch $S --out $S/shots

Échoue (exit ≠ 0) sur toute erreur serveur, assertion manquée ou erreur console JS. Les captures
sont ensuite RELUES par un humain (règle du projet : piloter ET voir).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ADMIN_PW = "lex-bench-admin-pw"

QUICK_TRANSCRIPT = """\
SPEAKER_00: Le projet Alfa avance avec l'Outil Bé et le nuage privé.
SPEAKER_01: Avec donc, comme Power, Nuage. On voit que l'Outil Bé se connecte au dépôt.
SPEAKER_00: Le développement avec LM Nuagé, puis retour dans le nuage de l'organisation.
SPEAKER_01: Mais à part ça, ça sera une vraie senteuse.
SPEAKER_00: le nuage, le nuage, le nuage : on y revient toujours.
"""

LEXICON = [
    {"id": "t1", "term": "Outil B", "category": "application", "priority": "critique",
     "variants": ["Outil Bé"], "replace_by": "", "comment": "", "contexts": [], "source": "llm"},
    {"id": "t2", "term": "nuage", "category": "technique", "priority": "normale",
     "variants": ["Nuagé"], "replace_by": "", "comment": "", "contexts": [], "source": "llm"},
    {"id": "t3", "term": "comme on peut voir, Nuage", "category": "mot suspect", "priority": "normale",
     "variants": ["comme Power", "Nuage"], "replace_by": "", "comment": "", "contexts": [], "source": "llm"},
    {"id": "t4", "term": "senteuse", "category": "mot suspect", "priority": "normale",
     "variants": ["utilisatrice"], "replace_by": "", "comment": "", "contexts": [], "source": "llm"},
    {"id": "t5", "term": "Polyzia", "category": "mot suspect", "priority": "normale",
     "variants": [], "replace_by": "", "comment": "mot isolé, sens non identifiable", "contexts": [], "source": "llm"},
]


def cmd_seed(args) -> int:
    scratch = Path(args.config).resolve().parent
    (scratch / "jobs").mkdir(parents=True, exist_ok=True)
    Path(args.config).write_text(f"""
runtime: {{role: all}}
auth: {{first_admin_username: admin, first_admin_password: {ADMIN_PW}}}
storage: {{jobs_dir: {scratch / 'jobs'}, shared_backend: fs}}
notifications: {{email: {{enabled: false}}}}
workflow:
  queue: {{enabled: false}}
""", encoding="utf-8")
    os.environ["TRANSCRIA_CONFIG"] = str(args.config)
    os.environ["TRANSCRIA_DATABASE_URL"] = f"sqlite:///{args.db}"
    os.environ.setdefault("TRANSCRIA_SECRET", "bench")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app import create_app

    app = create_app(str(args.config), start_background_services=False)
    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore

        admin = UserStore.get_by_username("admin")
        assert admin is not None, "admin non créé"
        job = JobStore.create_job(admin.id, title="Banc étape 6")
        fs = JobFilesystem(str(scratch / "jobs"), job.id)
        fs.save_text("summary/quick_transcript.txt", QUICK_TRANSCRIPT)
        fs.save_json("context/meeting_context.json", {"title": "Banc étape 6", "meeting_type": "Formation",
                                                      "language": "fr", "termes_suspects": LEXICON})
        fs.save_json("context/session_lexicon.json", LEXICON)
        fs.save_json("speakers/speaker_mapping.json", {"mapping": {}, "speakers": []})
        JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
        (scratch / "job_id").write_text(job.id, encoding="utf-8")
    print(f"seed OK → {args.config} (job {job.id})")
    return 0


def cmd_walk(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch)
    job_id = (scratch / "job_id").read_text(encoding="utf-8").strip()
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
        page.on("dialog", lambda d: d.accept())

        def row(term: str):
            return page.locator("#lexicon-list .lexicon-row").filter(has=page.locator(f".lex-term[value='{term}']")).first

        def rows_by_term():
            return {page.input_value(f"#lexicon-list .lexicon-row:nth-child({i + 1}) .lex-term"): i
                    for i in range(page.locator("#lexicon-list .lexicon-row").count())}

        def shot(name: str, locator=None) -> None:
            if locator is None:
                page.screenshot(path=str(out / f"{name}.png"), full_page=True)
            else:
                locator.screenshot(path=str(out / f"{name}.png"))

        def hints(r) -> str:
            # text_content (pas inner_text) : le texte compte même si la section est repliée.
            return r.locator("[data-lex-hints]").text_content() or ""

        page.goto(f"{args.base_url}/login", wait_until="networkidle")
        page.fill("input[name=username]", args.user)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        page.goto(f"{args.base_url}/jobs/{job_id}", wait_until="networkidle")
        check("page : 5 fiches de lexique", page.locator("#lexicon-list .lexicon-row").count() == 5)
        def open_step() -> None:
            # Étape repliée dans un <details> (préparation optionnelle) : l'ouvrir pour VOIR.
            lex = page.locator("#lexicon-list")
            if not lex.is_visible():
                page.locator("details:has(#lexicon-list) > summary").first.click()
            lex.scroll_into_view_if_needed()

        open_step()
        lex = page.locator("#lexicon-list")
        check("page : étape 6 visible", lex.is_visible())

        # 1. Aperçu du sens : la fiche saine montre ses compteurs.
        r1 = page.locator("#lexicon-list .lexicon-row").nth(0)
        h1 = hints(r1)
        check("aperçu : « Outil Bé » → « Outil B » : 2 occurrence(s)", "« Outil Bé » → « Outil B » : 2 occurrence(s)" in h1, h1)
        check("aperçu : fiche saine sans avertissement", "lex-row-warn" not in (r1.get_attribute("class") or ""))

        # 2. Forme-phrase + variante = forme validée d'une autre ligne (t3).
        r3 = page.locator("#lexicon-list .lexicon-row").nth(2)
        h3 = hints(r3)
        check("t3 : avertissement forme-phrase", "ressemble à une phrase" in h3, h3)
        check("t3 : « Nuage » = forme validée d'une autre ligne", "« Nuage » est la forme validée d'une autre ligne" in h3, h3)
        check("t3 : fiche marquée", "lex-row-warn" in (r3.get_attribute("class") or ""))

        # 3. Fiche retournée de travers (t4) : 0 occurrence de la variante, 1 de la forme.
        r4 = page.locator("#lexicon-list .lexicon-row").nth(3)
        h4 = hints(r4)
        check("t4 : « utilisatrice » : 0 occurrence", "« utilisatrice » → « senteuse » : 0 occurrence(s)" in h4, h4)
        check("t4 : question « à l'envers ? »", "la ligne est-elle à l'envers ?" in h4, h4)

        # 4. Mot fréquent (t2 après saisie d'une variante courante).
        r2 = page.locator("#lexicon-list .lexicon-row").nth(1)
        shot("01_etape6_initiale", lex)

        # 5. Fiche à l'envers (t5, « mot suspect » sans variante) : bouton mis en avant, échange.
        r5 = page.locator("#lexicon-list .lexicon-row").nth(4)
        check("t5 : bouton d'échange mis en avant", "lex-row-swap-first" in (r5.get_attribute("class") or ""))
        r5.locator(".lex-swap").click()
        check("t5 : la faute est passée en variante", r5.locator(".lex-variants").input_value() == "Polyzia")
        check("t5 : forme validée vidée, curseur dedans",
              r5.locator(".lex-term").input_value() == "" and page.evaluate("document.activeElement.classList.contains('lex-term')"))
        r5.locator(".lex-term").fill("Polysia")
        h5 = hints(r5)
        check("t5 : aperçu « Polyzia » → « Polysia » : 0 occurrence", "« Polyzia » → « Polysia » : 0 occurrence(s)" in h5, h5)
        check("t5 : pas de question « à l'envers » (forme absente aussi)", "à l'envers" not in h5, h5)
        shot("02_echange_fait", r5)

        # 6. Mot courant saisi en variante (t2) : avertissement de fréquence, en direct.
        r2.locator(".lex-variants").fill("Nuagé, nuage")
        h2 = hints(r2)
        check("t2 : variante égale à la forme ignorée (nuage = nuage)", "« nuage » →" not in h2, h2)
        r2.locator(".lex-variants").fill("Nuagé, dépôt")
        r2.locator(".lex-term").fill("nuage privé")
        h2 = hints(r2)
        check("t2 : compteur « Nuagé » : 1 occurrence", "« Nuagé » → « nuage privé » : 1 occurrence(s)" in h2, h2)
        r2.locator(".lex-term").fill("nuage")
        r2.locator(".lex-variants").fill("Nuagé, le")
        h2 = hints(r2)
        check("t2 : « le » fréquent → avertissement", "« le » apparaît" in h2 and "mot courant" in h2, h2)
        shot("03_mot_courant", r2)
        r2.locator(".lex-variants").fill("Nuagé")

        # 7. Sauvegarde puis relecture : l'échange est persistant, les indices se recalculent.
        page.click("[data-action='TranscrIA.saveLexicon']")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(900)
        page.goto(f"{args.base_url}/jobs/{job_id}", wait_until="networkidle")
        open_step()
        terms = rows_by_term()
        check("relecture : « Polysia » enregistrée", "Polysia" in terms, str(sorted(terms)))
        if "Polysia" in terms:
            rp = page.locator("#lexicon-list .lexicon-row").nth(terms["Polysia"])
            check("relecture : variante « Polyzia » conservée", rp.locator(".lex-variants").input_value() == "Polyzia")
        shot("04_apres_rechargement", page.locator("#lexicon-list"))

        browser.close()

    check("aucune erreur serveur (≥ 500)", not server_errors, "; ".join(server_errors[:3]))
    check("aucune erreur console JS", not console_errors, "; ".join(console_errors[:3]))
    print(f"\n{len(failures)} échec(s) — captures dans {out}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--config", required=True)
    s.add_argument("--db", required=True)
    s.set_defaults(fn=cmd_seed)
    w = sub.add_parser("walk")
    w.add_argument("--base-url", default="http://localhost:7899")
    w.add_argument("--scratch", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--user", default="admin")
    w.add_argument("--password", default=ADMIN_PW)
    w.set_defaults(fn=cmd_walk)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

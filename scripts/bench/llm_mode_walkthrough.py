#!/usr/bin/env python3
"""Banc Playwright de l'écran « LLM d'arbitrage » (/admin/llm-cloud) — GESTES réels, sans GPU.

Instance JETABLE (config scratch, SQLite, .env et opencode.json scratch) — jamais le service de
production. Vérifie les trois modes déclarés : saisie « serveur de mon organisation » avec clé
(clé jamais réaffichée, écrite dans le .env scratch), saisie invalide (400 sans écriture),
bascule « fournisseur externe », retour « sur cette machine », bouton de redémarrage (chemin
d'échec hors root : la commande manuelle s'affiche ; JAMAIS cliqué en root).

    S=/tmp/llm_bench ; mkdir -p $S
    venv/bin/python scripts/bench/llm_mode_walkthrough.py seed --config $S/config.yaml --db $S/app.db
    venv/bin/python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
        --server "TRANSCRIA_CONFIG=$S/config.yaml TRANSCRIA_DATABASE_URL=sqlite:///$S/app.db TRANSCRIA_SECRET=bench \\
                  OPENCODE_CONFIG=$S/opencode.json venv/bin/python app.py --port 7899" --port 7899 \\
        -- venv/bin/python scripts/bench/llm_mode_walkthrough.py walk --base-url http://localhost:7899 \\
           --scratch $S --out $S/shots

Échoue (exit ≠ 0) sur toute erreur serveur, assertion manquée ou erreur console JS. Les captures
sont ensuite RELUES par un humain (règle du projet : piloter ET voir).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ADMIN_PW = "llm-bench-admin-pw"
SECRET = "cle-de-banc-jamais-affichee-0123"


def cmd_seed(args) -> int:
    scratch = Path(args.config).resolve().parent
    (scratch / "jobs").mkdir(parents=True, exist_ok=True)
    (scratch / "agent_work").mkdir(exist_ok=True)
    Path(args.config).write_text(f"""
runtime: {{role: all}}
auth: {{first_admin_username: admin, first_admin_password: {ADMIN_PW}}}
storage: {{jobs_dir: {scratch / 'jobs'}, agent_work_dir: {scratch / 'agent_work'}, shared_backend: fs}}
notifications: {{email: {{enabled: false}}}}
services:
  backend: script
  arbitrage_llm_host: 127.0.0.1
  arbitrage_llm_port: 8080
  arbitrage_api_model_id: arbitrage
  arbitrage_script: ./scripts/launch_arbitrage.sh
workflow:
  arbitration_llm: {{enabled: true, model_id: local/arbitrage, api_base: "http://127.0.0.1:8080/v1", api_key_env: ""}}
  summary_llm: {{enabled: true, model_id: local/arbitrage, api_base: "http://127.0.0.1:8080/v1"}}
  queue: {{enabled: false}}
""", encoding="utf-8")
    (scratch / ".env").write_text("TRANSCRIA_SECRET=bench\n", encoding="utf-8")
    os.environ["TRANSCRIA_CONFIG"] = str(args.config)
    os.environ["TRANSCRIA_DATABASE_URL"] = f"sqlite:///{args.db}"
    os.environ.setdefault("TRANSCRIA_SECRET", "bench")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app import create_app

    app = create_app(str(args.config), start_background_services=False)
    with app.app_context():
        from transcria.auth.store import UserStore

        assert UserStore.get_by_username("admin") is not None, "admin non créé"
    print(f"seed OK → {args.config}")
    return 0


def cmd_walk(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch)
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

        def shot(name: str) -> None:
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)

        page.goto(f"{args.base_url}/login", wait_until="networkidle")
        page.fill("input[name=username]", args.user)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        # 1. Écran initial : mode local coché, champs des autres modes masqués
        page.goto(f"{args.base_url}/admin/llm-cloud", wait_until="networkidle")
        check("écran : mode local coché", page.is_checked("#llm-mode-local"))
        check("écran : champs organisation masqués", not page.is_visible("#org-api-base"))
        check("écran : aucun fournisseur externe actif", page.is_visible("[data-llm-cloud-inactive]"))
        shot("01_local_initial")

        # 2. Mode organisation : champs révélés, saisie avec clé
        page.check("#llm-mode-organisation")
        check("organisation : champs révélés", page.is_visible("#org-api-base") and page.is_visible("#org-api-key"))
        page.fill("#org-api-base", "https://llm.exemple.local:8443")
        page.fill("#org-model-alias", "arbitrage")
        page.fill("#org-api-key", SECRET)
        shot("02_organisation_saisie")
        page.click("#llm-mode-form button[type=submit]")
        page.wait_for_load_state("networkidle")
        html = page.content()
        check("organisation : confirmation « LLM locale »", "Serveur de votre organisation enregistré" in html and "LLM locale" in html)
        check("organisation : bandeau redémarrage requis", page.is_visible("[data-llm-pending-restart]"))
        check("organisation : clé JAMAIS réaffichée", SECRET not in html)
        check("organisation : champ clé vide, indication « déjà enregistrée »",
              page.input_value("#org-api-key") == "" and "déjà enregistrée" in (page.get_attribute("#org-api-key", "placeholder") or ""))
        env_text = (scratch / ".env").read_text(encoding="utf-8")
        check("organisation : clé écrite dans le .env scratch", f"TRANSCRIA_ARBITRAGE_API_KEY='{SECRET}'" in env_text)
        check("organisation : .env en 0600", oct((scratch / ".env").stat().st_mode & 0o777) == "0o600")
        cfg_text = (scratch / "config.yaml").read_text(encoding="utf-8")
        check("organisation : config.yaml backend http + https",
              "backend: http" in cfg_text and "https://llm.exemple.local:8443/v1" in cfg_text)
        check("organisation : config.yaml sans la clé", SECRET not in cfg_text)
        oc = scratch / "opencode.json"
        oc_text = oc.read_text(encoding="utf-8") if oc.exists() else ""
        check("organisation : opencode.json {env:VAR}", "{env:TRANSCRIA_ARBITRAGE_API_KEY}" in oc_text and SECRET not in oc_text)
        shot("03_organisation_enregistree")

        # 2 bis. Redémarrer : hors root, le one-shot échoue → commande manuelle (jamais cliqué en root)
        if os.geteuid() != 0 and page.is_visible("[data-llm-pending-restart] button"):
            page.click("[data-llm-pending-restart] button")
            page.wait_for_load_state("networkidle")
            html = page.content()
            check("redémarrage : repli commande manuelle", "sudo systemctl restart transcria" in html)
            server_errors[:] = [e for e in server_errors if "/admin/llm-cloud/restart" not in e]   # 500 attendu ici
            console_errors[:] = [e for e in console_errors if "status of 500" not in e]
            shot("03b_redemarrage_repli")
        else:
            print("[SKIP] redémarrage (root ou bandeau absent)")
        page.goto(f"{args.base_url}/admin/llm-cloud", wait_until="networkidle")


        # 3. Saisie invalide : 400, rien d'écrit, la clé soumise n'apparaît pas dans la page d'erreur
        before = cfg_text
        page.check("#llm-mode-organisation")
        page.fill("#org-api-base", "http://utilisateur:secret@llm.exemple.local/v1")   # type=url accepte, le serveur refuse
        page.fill("#org-api-key", SECRET + "-bis")
        page.click("#llm-mode-form button[type=submit]")
        page.wait_for_load_state("networkidle")
        html = page.content()
        check("invalide : message FR qui guide", "Adresse du serveur invalide" in html)
        check("invalide : la clé soumise n'est pas réaffichée", (SECRET + "-bis") not in html)
        check("invalide : config inchangée", (scratch / "config.yaml").read_text(encoding="utf-8") == before)
        console_errors[:] = [e for e in console_errors if "status of 400" not in e]   # le 400 attendu ici
        shot("04_saisie_invalide")

        # 4. Fournisseur externe : avertissement visible, champs cloud, soumission avec clé
        page.goto(f"{args.base_url}/admin/llm-cloud", wait_until="networkidle")
        page.check("#llm-mode-cloud")
        check("cloud : champs révélés", page.is_visible("#cloud-provider") and page.is_visible("#cloud-api-key"))
        check("cloud : avertissement « moins maîtrisé »", "Moins maîtrisé qu" in page.content())
        page.select_option("#cloud-provider", "mistral")
        page.fill("#cloud-model", "mistral-large-4")
        page.fill("#cloud-api-key", SECRET + "-cloud")
        shot("05_cloud_saisie")
        page.click("#llm-mode-form button[type=submit]")
        page.wait_for_load_state("networkidle")
        html = page.content()
        check("cloud : confirmation + validation annoncée", "Fournisseur externe enregistré" in html and "validez ce fournisseur" in html)
        check("cloud : clé jamais réaffichée", (SECRET + "-cloud") not in html)
        check("cloud : config backend cloud + photographie locale",
              "backend: cloud" in (scratch / "config.yaml").read_text(encoding="utf-8")
              and "local_llm_snapshot" in (scratch / "config.yaml").read_text(encoding="utf-8"))
        shot("06_cloud_enregistre")

        # 5. Retour sur cette machine : la photographie est remise
        page.check("#llm-mode-local")
        page.click("#llm-mode-form button[type=submit]")
        page.wait_for_load_state("networkidle")
        html = page.content()
        check("local : confirmation", "LLM sur cette machine rétablie" in html)
        cfg_text = (scratch / "config.yaml").read_text(encoding="utf-8")
        check("local : config backend script, hôte 127.0.0.1",
              "backend: script" in cfg_text and "arbitrage_llm_host: 127.0.0.1" in cfg_text)
        shot("07_retour_local")

        browser.close()

    check("aucune erreur serveur (≥ 500)", not server_errors, "; ".join(server_errors[:3]))
    check("aucune erreur console JS", not console_errors, "; ".join(console_errors[:3]))
    print(f"\n{len(failures)} échec(s) ; captures → {out}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--config", required=True)
    s.add_argument("--db", required=True)
    s.set_defaults(func=cmd_seed)
    w = sub.add_parser("walk")
    w.add_argument("--base-url", required=True)
    w.add_argument("--scratch", required=True)
    w.add_argument("--user", default="admin")
    w.add_argument("--password", default=ADMIN_PW)
    w.add_argument("--out", required=True)
    w.set_defaults(func=cmd_walk)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

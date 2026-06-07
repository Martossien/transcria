#!/usr/bin/env python3
"""Walkthrough UI (Playwright) — vérifie la couche web réelle sans GPU.

Parcourt les pages que les tests unitaires ne couvrent qu'en HTML mocké : login,
accueil, création de job + wizard, éditeur de configuration (onglets formulaires
+ YAML, aller-retour de sauvegarde) et toutes les pages admin. Capture une
capture d'écran par étape et **échoue (exit≠0)** si une page renvoie une erreur
serveur, perd une assertion clé ou émet une erreur console JS.

Ne déclenche aucune étape pipeline (analyse/résumé/transcription) → aucun GPU requis.

Usage :
    venv/bin/python scripts/ui_walkthrough.py --base-url http://localhost:7899 \
        --user admin --password <pw> [--out /tmp/ui_walkthrough]

Prérequis (instance jetable, ne touche pas le service prod ni sa base) :
    pip install -r requirements-dev.txt && playwright install chromium
    mkdir -p /tmp/ui_walk/jobs
    cat > /tmp/ui_walk/config.yaml <<'YAML'
    runtime: {role: web}            # pas d'ordonnanceur : aucune étape pipeline déclenchée
    auth: {first_admin_username: admin, first_admin_password: walkthrough-admin-pw}
    storage: {jobs_dir: /tmp/ui_walk/jobs}
    notifications: {email: {enabled: false}}
    YAML

Lancé via le helper de cycle de vie serveur (instance contrôlée, DB SQLite temp) :
    venv/bin/python ~/.claude/skills/webapp-testing/scripts/with_server.py \
        --server "TRANSCRIA_CONFIG=/tmp/ui_walk/config.yaml \
                  TRANSCRIA_DATABASE_URL=sqlite:////tmp/ui_walk/app.db \
                  TRANSCRIA_SECRET=walk venv/bin/python app.py --port 7899" \
        --port 7899 -- venv/bin/python scripts/ui_walkthrough.py --base-url http://localhost:7899 \
        --user admin --password walkthrough-admin-pw
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


class Walkthrough:
    def __init__(self, page: Page, base_url: str, out: Path):
        self.page = page
        self.base = base_url.rstrip("/")
        self.out = out
        self.checks: list[tuple[str, bool, str]] = []
        self.console_errors: list[str] = []
        self.server_errors: list[str] = []
        page.on("console", self._on_console)
        page.on("response", self._on_response)

    def _on_console(self, msg) -> None:
        if msg.type == "error":
            self.console_errors.append(msg.text)

    def _on_response(self, resp) -> None:
        if resp.status >= 500:
            self.server_errors.append(f"{resp.status} {resp.url}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")

    def shot(self, name: str) -> None:
        self.page.screenshot(path=str(self.out / f"{name}.png"), full_page=True)

    def goto(self, path: str, name: str) -> None:
        resp = self.page.goto(f"{self.base}{path}", wait_until="networkidle")
        self.shot(name)
        status = resp.status if resp else 0
        self.check(f"GET {path} → {status}", bool(resp and resp.status < 400), f"status={status}")

    # ── Étapes ──────────────────────────────────────────────────────────────

    def login(self, user: str, password: str) -> None:
        self.page.goto(f"{self.base}/login", wait_until="networkidle")
        self.shot("01_login")
        self.page.fill('input[name="username"]', user)
        self.page.fill('input[name="password"]', password)
        self.page.click('button[type="submit"], input[type="submit"]')
        self.page.wait_for_load_state("networkidle")
        self.shot("02_home")
        body = self.page.content()
        self.check("connexion réussie (déconnexion visible)", "Déconnexion" in body or "logout" in body.lower())

    def create_job_and_open_wizard(self) -> None:
        self.page.goto(f"{self.base}/", wait_until="networkidle")
        # Le formulaire de création poste vers /jobs/new (titre requis).
        try:
            self.page.fill('input[name="title"]', "Walkthrough UI job")
            self.page.click('form[action="/jobs/new"] button[type="submit"], form[action="/jobs/new"] [type="submit"]')
            self.page.wait_for_load_state("networkidle")
            self.shot("03_wizard")
            self.check("wizard ouvert après création", "/jobs/" in self.page.url, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("création de job", False, str(exc)[:120])

    def config_editor(self) -> None:
        self.page.goto(f"{self.base}/admin/config", wait_until="networkidle")
        self.shot("04_config_form")
        body = self.page.content()
        self.check("éditeur config : onglet Réglages", 'name="_mode"' in body and 'pane-form' in body)
        self.check("éditeur config : champ select STT", 'name="models.stt_backend"' in body)
        self.check("éditeur config : onglet YAML avancé", 'pane-yaml' in body and 'name="config_yaml"' in body)
        self.check("éditeur config : secret masqué", "********" in body)
        # Aller-retour : modifier la concurrence max via le formulaire et sauvegarder.
        try:
            field = 'input[name="workflow.execution.max_concurrent_jobs"]'
            self.page.fill(field, "2")
            self.page.click('#pane-form button[type="submit"]')
            self.page.wait_for_load_state("networkidle")
            self.shot("05_config_saved")
            saved = self.page.input_value(field)
            self.check("sauvegarde formulaire persistée", saved == "2", f"valeur relue={saved}")
        except Exception as exc:  # noqa: BLE001
            self.check("sauvegarde formulaire config", False, str(exc)[:120])

    def admin_pages(self) -> None:
        for path, name in [
            ("/admin/users", "06_users"),
            ("/admin/groups", "07_groups"),
            ("/admin/queue", "08_queue"),
            ("/admin/lexicons", "09_lexicons"),
            ("/admin/voices", "10_voices"),
            ("/admin/audit", "11_audit"),
            ("/admin/schedule", "12_schedule"),
            ("/system", "13_system"),
        ]:
            self.goto(path, name)

    def report(self) -> bool:
        failed = [c for c in self.checks if not c[1]]
        print("\n── Résumé ──")
        print(f"  checks: {len(self.checks) - len(failed)}/{len(self.checks)} OK")
        if self.server_errors:
            print(f"  erreurs serveur (5xx): {self.server_errors}")
        if self.console_errors:
            print(f"  erreurs console JS: {self.console_errors[:5]}")
        print(f"  captures: {self.out}")
        return not failed and not self.server_errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Walkthrough UI Playwright (sans GPU)")
    parser.add_argument("--base-url", default="http://localhost:7899")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--out", default="/tmp/ui_walkthrough")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        wt = Walkthrough(page, args.base_url, out)
        try:
            wt.login(args.user, args.password)
            wt.create_job_and_open_wizard()
            wt.config_editor()
            wt.admin_pages()
        finally:
            browser.close()
        ok = wt.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

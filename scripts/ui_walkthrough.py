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
import json
import sys
import time
import wave
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def _write_tiny_wav(path: Path) -> None:
    """WAV d'une seconde de silence : suffit à l'upload (aucune étape GPU déclenchée)."""
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)


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
            # Exigence ferme : le choix du profil est posé DÈS l'étape 1 (upload), car il
            # pilote quelles étapes suivent (cf. docs/archive/RELEASE_0.2.0.md — profil à l'étape 1).
            first_section = self.page.locator(".step-section").first
            self.check(
                "profil de traitement dans la 1ʳᵉ étape du wizard",
                first_section.locator("#profile-selector").count() > 0,
            )
            self.check(
                "au moins un profil proposé à l'étape 1",
                self.page.locator(".profile-pill").count() > 0,
            )
            # Le profil se choisit APRÈS le téléversement : tant qu'aucun fichier
            # n'est reçu, toutes les pastilles sont verrouillées (désactivées).
            self.check(
                "profils verrouillés avant téléversement",
                self.page.locator(".profile-pill:not([disabled])").count() == 0,
            )
            # Téléverser un WAV minimal (l'upload ne déclenche aucune étape GPU)
            # pour déverrouiller le choix du profil.
            wav_path = self.out / "walkthrough_upload.wav"
            _write_tiny_wav(wav_path)
            self.page.set_input_files("#file-upload", str(wav_path))
            self.page.click('button:has-text("Téléverser")')
            self.page.wait_for_selector(".profile-pill:not([disabled])", timeout=20000)
            self.shot("03a_after_upload")
            self.check(
                "profils déverrouillés après téléversement",
                self.page.locator(".profile-pill:not([disabled])").count() > 0,
            )
            # Interaction réelle : choisir un autre profil disponible doit persister
            # (chooseProfile POST /api/jobs/<id>/profile puis reload → data-selected MAJ).
            alt = self.page.locator(".profile-pill:not([disabled]):not(.active)")
            if alt.count() > 0:
                target_id = alt.first.get_attribute("data-profile-id")
                alt.first.click()
                self.page.wait_for_load_state("networkidle")
                self.shot("03b_profile_switched")
                new_sel = self.page.locator("#profile-selector").get_attribute("data-selected")
                self.check(
                    "changement de profil persisté (POST + reload)",
                    new_sel == target_id,
                    f"attendu={target_id}, obtenu={new_sel}",
                )
            else:
                print("  [skip] interaction profil : <2 profils disponibles sur cette instance")
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
        # Chaque onglet est vérifié sur son CONTENU attendu (un marqueur stable du
        # template), pas seulement sur un « 200 OK » — cf. docs/archive/RELEASE_0.2.0.md §3.2.
        for path, name, marker in [
            ("/admin/users", "06_users", "Gestion des utilisateurs"),
            ("/admin/groups", "07_groups", "Groupes"),
            ("/admin/queue", "08_queue", "File d'attente"),
            ("/admin/lexicons", "09_lexicons", "Lexiques centralisés"),
            ("/admin/voices", "10_voices", "Voix enregistrées"),
            ("/admin/audit", "11_audit", "Audit de sécurité"),
            ("/admin/schedule", "12_schedule", "Planification"),
            ("/system", "13_system", "État technique du système"),
        ]:
            resp = self.page.goto(f"{self.base}{path}", wait_until="networkidle")
            self.shot(name)
            status = resp.status if resp else 0
            has_marker = marker in self.page.content()
            ok = bool(resp and resp.status < 400) and has_marker
            self.check(
                f"{path} : contenu attendu (« {marker} »)",
                ok,
                f"status={status}, marqueur={'présent' if has_marker else 'ABSENT'}",
            )

    def admin_crud(self) -> None:
        # Au-delà du rendu : on CRÉE réellement une entité par onglet et on vérifie sa
        # persistance (la page de destination doit contenir ce qu'on vient de créer).
        # Instance jetable → écritures sans impact. Suffixe horodaté = pas de collision.
        suffix = str(int(time.time()))

        # ── Utilisateur ──
        try:
            uname = f"walk_user_{suffix}"
            self.page.goto(f"{self.base}/admin/users/new", wait_until="networkidle")
            self.page.fill('input[name="username"]', uname)
            self.page.fill('input[name="display_name"]', "Walkthrough User")
            self.page.fill('input[name="password"]', "walkpass1234")
            self.page.fill('input[name="password_confirm"]', "walkpass1234")
            # Cibler le bouton « Créer » par son libellé : base.html expose aussi un
            # bouton submit « Déconnexion » dans la navbar (un sélecteur générique
            # button[type=submit] matcherait les deux).
            self.page.get_by_role("button", name="Créer").click()
            self.page.wait_for_load_state("networkidle")
            self.shot("14_user_created")
            ok = uname in self.page.content() and self.page.url.rstrip("/").endswith("/admin/users")
            self.check("CRUD utilisateur : créé et listé", ok, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("CRUD utilisateur", False, str(exc)[:120])

        # ── Groupe ──
        try:
            gname = f"Walk Group {suffix}"
            self.page.goto(f"{self.base}/admin/groups/new", wait_until="networkidle")
            self.page.fill('input[name="name"]', gname)
            self.page.fill('input[name="description"]', "Groupe créé par le walkthrough")
            # Cibler le bouton « Créer » par son libellé : base.html expose aussi un
            # bouton submit « Déconnexion » dans la navbar (un sélecteur générique
            # button[type=submit] matcherait les deux).
            self.page.get_by_role("button", name="Créer").click()
            self.page.wait_for_load_state("networkidle")
            self.shot("15_group_created")
            ok = gname in self.page.content() and "/admin/groups/" in self.page.url
            self.check("CRUD groupe : créé et ouvert", ok, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("CRUD groupe", False, str(exc)[:120])

        # ── Lexique centralisé ──
        try:
            lname = f"Walk Lexicon {suffix}"
            self.page.goto(f"{self.base}/admin/lexicons/new", wait_until="networkidle")
            self.page.fill('input[name="name"]', lname)
            desc = self.page.locator('[name="description"]')
            if desc.count() > 0:
                desc.first.fill("Lexique walkthrough")
            # Cibler le bouton « Créer » par son libellé : base.html expose aussi un
            # bouton submit « Déconnexion » dans la navbar (un sélecteur générique
            # button[type=submit] matcherait les deux).
            self.page.get_by_role("button", name="Créer").click()
            self.page.wait_for_load_state("networkidle")
            self.shot("16_lexicon_created")
            ok = lname in self.page.content() and "/admin/lexicons/" in self.page.url
            self.check("CRUD lexique : créé et ouvert", ok, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("CRUD lexique", False, str(exc)[:120])

    def voice_crud(self) -> None:
        # Enrôlement d'un sujet voix = métadonnées seules (l'embedding, qui exige
        # audio + modèle, est une étape /generate séparée) → testable sans GPU.
        # group_id laissé sur « Global » (config jetable : allow_global_profiles=true).
        try:
            vname = f"Voix Walk {int(time.time())}"
            self.page.goto(f"{self.base}/admin/voices/new", wait_until="networkidle")
            self.page.fill('input[name="display_name"]', vname)
            self.page.select_option('select[name="gender"]', "female")
            self.page.get_by_role("button", name="Créer").click()
            self.page.wait_for_load_state("networkidle")
            self.shot("19_voice_created")
            ok = vname in self.page.content() and "/admin/voices/" in self.page.url
            self.check("CRUD voix : sujet créé et ouvert", ok, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("CRUD voix", False, str(exc)[:120])

    def auth_flows(self) -> None:
        # Sécurité : login invalide rejeté, RBAC (un opérateur n'accède pas à l'admin),
        # et self-service mot de passe. Sessions isolées dans des contextes dédiés pour
        # ne pas perturber la session admin de self.page.
        browser = self.page.context.browser

        # 1) Login invalide → reste sur /login avec message d'erreur (pas de redirection).
        ctx = browser.new_context()
        try:
            p2 = ctx.new_page()
            p2.goto(f"{self.base}/login", wait_until="networkidle")
            p2.fill('input[name="username"]', "admin")
            p2.fill('input[name="password"]', "mauvais-mot-de-passe")
            p2.click('button[type="submit"], input[type="submit"]')
            p2.wait_for_load_state("networkidle")
            ok = p2.url.rstrip("/").endswith("/login") and "incorrect" in p2.content().lower()
            self.check("login invalide rejeté (reste sur /login + erreur)", ok, p2.url)
        except Exception as exc:  # noqa: BLE001
            self.check("login invalide", False, str(exc)[:120])
        finally:
            ctx.close()

        # 2) Créer un opérateur (rôle explicite, l'enum Role commence par ADMIN).
        op_user = f"walk_op_{int(time.time())}"
        op_pw = "operatorpass1"
        try:
            self.page.goto(f"{self.base}/admin/users/new", wait_until="networkidle")
            self.page.fill('input[name="username"]', op_user)
            self.page.select_option('select[name="role"]', "operator")
            self.page.fill('input[name="password"]', op_pw)
            self.page.fill('input[name="password_confirm"]', op_pw)
            self.page.get_by_role("button", name="Créer").click()
            self.page.wait_for_load_state("networkidle")
            self.check("opérateur créé (rôle operator) pour test RBAC", op_user in self.page.content())
        except Exception as exc:  # noqa: BLE001
            self.check("création opérateur (RBAC)", False, str(exc)[:120])
            return

        # 3) Session opérateur : RBAC (bloqué sur /admin) + changement de son mot de passe.
        ctx = browser.new_context()
        try:
            op = ctx.new_page()
            op.goto(f"{self.base}/login", wait_until="networkidle")
            op.fill('input[name="username"]', op_user)
            op.fill('input[name="password"]', op_pw)
            op.click('button[type="submit"], input[type="submit"]')
            op.wait_for_load_state("networkidle")

            resp = op.goto(f"{self.base}/admin/users", wait_until="networkidle")
            status = resp.status if resp else 0
            denied = status == 403 or "Gestion des utilisateurs" not in op.content()
            self.check("RBAC : opérateur bloqué sur /admin/users", denied, f"status={status}")

            op.goto(f"{self.base}/account/password", wait_until="networkidle")
            op.fill('input[name="current_password"]', op_pw)
            op.fill('input[name="new_password"]', "operatorpass2")
            op.fill('input[name="confirm_password"]', "operatorpass2")
            op.get_by_role("button", name="Mettre à jour").click()
            op.wait_for_load_state("networkidle")
            self.shot("17_password_changed")
            self.check("changement de mot de passe (self-service)", "mis à jour" in op.content().lower())
        except Exception as exc:  # noqa: BLE001
            self.check("RBAC / self-service mot de passe", False, str(exc)[:120])
        finally:
            ctx.close()

    def job_result_page(self, job_id: str) -> None:
        # Page de livrables d'un job TERMINÉ (seedé hors-ligne, sans GPU) : badge
        # « Terminé », aperçu SRT, et les trois liens de téléchargement (srt/docx/zip).
        try:
            self.page.goto(f"{self.base}/jobs/{job_id}/result", wait_until="networkidle")
            self.shot("18_job_result")
            body = self.page.content()
            ok = (
                'badge bg-success">Terminé' in body
                and "srt-preview" in body
                and f"/api/jobs/{job_id}/download/srt" in body
                and f"/api/jobs/{job_id}/download/docx" in body
                and f"/api/jobs/{job_id}/download/package" in body
            )
            self.check("page /result : job terminé, SRT + liens téléchargement", ok, self.page.url)
        except Exception as exc:  # noqa: BLE001
            self.check("page /result", False, str(exc)[:120])

    def meeting_types_page(self) -> None:
        # Types de réunion personnalisés (lot E) : galerie des 18 intégrés, éditeur
        # duplicate-first, création réelle d'un type, retour en galerie. GPU-free.
        try:
            self.page.goto(f"{self.base}/meeting-types", wait_until="networkidle")
            self.page.wait_for_selector(".mt-card", timeout=8000)
            self.shot("20_meeting_types_galerie")
            cards = self.page.locator(".mt-card").count()
            self.check("types de réunion : galerie affichée (≥ 18 cartes)", cards >= 18, f"{cards} cartes")

            # Dupliquer un intégré → l'éditeur s'ouvre avec l'aperçu vivant.
            self.page.locator('[data-action="duplicate"]', has_text="Créer le mien").first.click()
            self.page.wait_for_selector("#mt-editor:not(.d-none)", timeout=5000)
            self.shot("21_meeting_types_editeur")
            preview_ok = self.page.locator("#mt-cover-banner").is_visible()
            self.check("types de réunion : éditeur + aperçu de couverture", preview_ok, self.page.url)

            # Créer un type réel (nom unique) et le retrouver en galerie.
            name = f"Walkthrough {int(time.time())}"
            self.page.fill("#mt-name", name)
            self.page.fill("#mt-banner", "COMPTE-RENDU — WALKTHROUGH")
            self.page.click("#mt-save")
            self.page.wait_for_selector("#mt-gallery:not(.d-none)", timeout=8000)
            created = name in self.page.content()
            self.check("types de réunion : création puis visible en galerie", created, name)
            self.shot("22_meeting_types_cree")
        except Exception as exc:  # noqa: BLE001
            self.check("types de réunion", False, str(exc)[:120])

    def srt_editor_workshop(self, job_id: str) -> None:
        # Éditeur de transcription intégré (GPU-free) : atelier chargé, édition d'un
        # texte, version enregistrée dans le pool commun — sur le job seedé SANS audio
        # (le mode dégradé A1 est donc AUSSI couvert : bandeau + éditions possibles).
        try:
            self.page.goto(f"{self.base}/jobs/{job_id}/editor", wait_until="networkidle")
            self.page.wait_for_selector("#se-main:not(.d-none) .se-card, #se-resume:not(.d-none)", timeout=8000)
            if self.page.locator("#se-resume:not(.d-none)").count():
                self.page.click("#se-resume-no")
                self.page.wait_for_selector(".se-card", timeout=8000)
            self.shot("23_editeur_atelier")
            cards = self.page.locator(".se-card").count()
            body = self.page.content()
            self.check("éditeur : atelier chargé (cartes + bandeau sans-audio)",
                       cards >= 1 and "Audio non disponible" in body, f"{cards} segments")

            text_el = self.page.locator(".se-card .se-text").first
            text_el.click()
            self.page.keyboard.press("Control+a")
            self.page.keyboard.type("Texte edite par le walkthrough CI.")
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(300)
            edited = "Texte edite par le walkthrough CI." in self.page.locator(".se-card .se-text").first.inner_text()
            self.check("éditeur : le texte est le champ (édition sur place)", edited)

            self.page.click("#se-save")
            self.page.wait_for_timeout(1200)
            state_txt = self.page.locator("#se-save-state").inner_text()
            self.check("éditeur : version enregistrée (pool commun)", "version v" in state_txt, state_txt)
            self.shot("24_editeur_version")
        except Exception as exc:  # noqa: BLE001
            self.check("éditeur de transcription", False, str(exc)[:120])

    def states_deep_dive(self, ids: dict) -> None:
        """C0.1 (RELEASE_0.2.0 §2) : assertions de FOND sur données seedées — les pages
        admin passent de « marqueur de contenu » à « contenu attendu par état »."""
        # File d'attente : la file ne liste que les jobs EN FILE (pas tous les jobs) —
        # l'état couvert ici est donc la FILE VIDE avec ses compteurs lisibles.
        self.page.goto(f"{self.base}/admin/queue", wait_until="networkidle")
        body = self.page.content()
        self.check("file : état vide lisible (compteurs à 0)", "En attente" in body and "En cours" in body)
        raw_states = [s for s in ("summary_done", "ready_to_process", "lexicon_done") if s in body]
        self.check("file : aucun état brut affiché", not raw_states, ", ".join(raw_states))
        self.shot("30_queue_vide")

        # Utilisateurs : les 4 comptes rendus avec leur RÔLE lisible.
        self.page.goto(f"{self.base}/admin/users", wait_until="networkidle")
        body = self.page.content()
        self.check("users : comptes seedés + rôles rendus",
                   all(u in body for u in ("demo-lectrice", "demo-operateur", "demo-gestionnaire")))
        self.shot("31_users_peuples")

        # Groupes : les 2 groupes avec nombre de membres.
        self.page.goto(f"{self.base}/admin/groups", wait_until="networkidle")
        body = self.page.content()
        self.check("groupes : Direction + Secrétariat rendus",
                   "Direction" in body and "Secrétariat" in body)
        self.shot("32_groupes_peuples")

        # Lexiques centraux : 2 lexiques, compte d'entrées visible.
        self.page.goto(f"{self.base}/admin/lexicons", wait_until="networkidle")
        body = self.page.content()
        self.check("lexiques : les 2 lexiques seedés rendus",
                   "Vocabulaire interne" in body and "Termes financiers" in body)
        self.shot("33_lexiques_peuples")

        # Audit : des événements réels sont rendus (pas un tableau vide).
        self.page.goto(f"{self.base}/admin/audit", wait_until="networkidle")
        body = self.page.content()
        self.check("audit : événements seedés rendus (création utilisateur/groupe)",
                   "demo-operateur" in body or "user_create" in body or "Création" in body)
        self.shot("34_audit_peuple")

        # Accueil : la liste des jobs montre des ÉTATS FRANÇAIS variés.
        self.page.goto(f"{self.base}/", wait_until="networkidle")
        body = self.page.content()
        self.check("accueil : jobs multi-états rendus",
                   "Négociation fournisseur" in body or "Réunion client Nord" in body)
        self.check("accueil : aucun état brut", "ready_to_process" not in body and "summary_done" not in body)
        self.shot("35_accueil_peuple")

        # Planification (C3.6) : les 3 questions du gestionnaire ont leur panneau,
        # la frise hebdomadaire rend les créneaux seedés (segments serveur).
        self.page.goto(f"{self.base}/admin/schedule", wait_until="networkidle")
        body = self.page.content()
        self.check("planification : panneaux « maintenant » rendus",
                   "En cours de traitement" in body and "En attente" in body and "Agenda" in body)
        seg_count = self.page.locator(".sched-seg").count()
        self.check("planification : frise hebdomadaire avec segments", seg_count >= 5, f"{seg_count} segments")
        self.check("planification : bascule d'agenda visible",
                   self.page.locator("#schedule-master-toggle").count() == 1)
        self.shot("38_planification")

        # C3.1 — le wizard à CHAQUE état du workflow : étape courante marquée,
        # aucun état brut, un guidage visible (captures revues une à une).
        # indices dans job_ids = ordre de création du seed (_JOB_STATES)
        state_expectations = [
            (0, "creé/fichier", "Fichier audio"),
            (1, "résumé fait", "Contexte de la réunion"),
            (2, "contexte fait", "Participants"),
            (3, "lexique fait", "Traitement"),
            (4, "prêt", "Traitement"),
            (7, "échec", None),
        ]
        job_ids = ids.get("job_ids", [])
        for idx, (seed_idx, label, expected_marker) in enumerate(state_expectations):
            job_id = job_ids[seed_idx] if seed_idx < len(job_ids) else None
            if not job_id:
                self.check(f"wizard état « {label} » : job seedé retrouvé", False, str(seed_idx))
                continue
            self.page.goto(f"{self.base}/jobs/{job_id}", wait_until="networkidle")
            body = self.page.content()
            current = self.page.locator(".step-section.current-step").count()
            raw = [s for s in ("summary_done", "context_done", "lexicon_done",
                               "ready_to_process") if s in body]
            self.check(f"wizard « {label} » : étape courante marquée + zéro état brut",
                       current >= 1 and not raw,
                       f"current={current}" + (f", bruts={raw}" if raw else ""))
            if expected_marker:
                self.check(f"wizard « {label} » : la bonne étape est proposée",
                           expected_marker in body, expected_marker)
            self.shot(f"5{idx}_wizard_{label.replace(' ', '_').replace('/', '-')}")

        # Types de réunion : le type personnalisé seedé apparaît dans la galerie.
        self.page.goto(f"{self.base}/meeting-types", wait_until="networkidle")
        self.check("types : le type démo personnalisé est rendu",
                   "Réunion démo qualité" in self.page.content())

    def _logout(self) -> None:
        # /logout est POST-only (anti-CSRF de déconnexion) : on passe par le formulaire.
        self.page.goto(f"{self.base}/", wait_until="networkidle")
        self.page.evaluate("document.querySelector('form[action=\"/logout\"]').submit()")
        self.page.wait_for_url("**/login*", timeout=8000)

    def role_walkthroughs(self) -> None:
        """C0.1 : les pages vues À TRAVERS chaque rôle (pas seulement l'admin)."""
        # Lectrice (VIEWER) : lecture seule — l'admin est interdit, l'accueil lisible.
        self._logout()
        self.login("demo-lectrice", "walkthrough-demo-pw")
        resp = self.page.goto(f"{self.base}/admin/users", wait_until="networkidle")
        self.check("lectrice : /admin/users interdit", resp is not None and resp.status == 403)
        self.page.goto(f"{self.base}/", wait_until="networkidle")
        self.check("lectrice : accueil accessible", "Déconnexion" in self.page.content())
        self.shot("36_role_lectrice")

        # Opérateur : peut créer un job, pas d'administration.
        self._logout()
        self.login("demo-operateur", "walkthrough-demo-pw")
        resp = self.page.goto(f"{self.base}/admin/config", wait_until="networkidle")
        self.check("opérateur : /admin/config interdit", resp is not None and resp.status == 403)
        self.page.goto(f"{self.base}/", wait_until="networkidle")
        body = self.page.content()
        self.check("opérateur : création de job proposée", "Nouveau" in body or "nouveau traitement" in body.lower())
        self.shot("37_role_operateur")

        # Retour admin pour la suite du parcours.
        self._logout()
        self.login("admin", self.admin_password)

    def refine_chat_panel(self, job_id: str) -> None:
        # Chat d'affinage des livrables (GPU-free : AUCUN appel LLM ici) : le panneau
        # est présent sur /result, l'endpoint de polling répond, et les options de
        # rendu DIRECTES (sans assistant) sont acceptées puis reflétées par le GET.
        try:
            # La page résultats doit être ATTEIGNABLE depuis l'accueil (bouton
            # « Résultats » sur la carte du job terminé) — pas seulement par URL directe.
            self.page.goto(f"{self.base}/", wait_until="networkidle")
            self.check(
                "affinage : l'accueil relie la page résultats du job terminé",
                self.page.locator(f'a[href="/jobs/{job_id}/result"]').count() > 0,
            )
            self.page.goto(f"{self.base}/jobs/{job_id}/result", wait_until="networkidle")
            ok_panel = self.page.locator("#refine-chat").count() == 1
            resp = self.page.request.get(f"{self.base}/api/jobs/{job_id}/refine/chat")
            data = resp.json() if resp.ok else {}
            self.check(
                "affinage : panneau présent + endpoint chat répond",
                ok_panel and resp.ok and "turns" in data and "themes" in data,
                f"status={resp.status}",
            )
            r2 = self.page.request.post(
                f"{self.base}/api/jobs/{job_id}/refine/render-options",
                data=json.dumps({"sections": {"transcript": False}}),
                headers={"Content-Type": "application/json"},
            )
            r3 = self.page.request.get(f"{self.base}/api/jobs/{job_id}/refine/chat")
            opts = (r3.json() if r3.ok else {}).get("render_options", {})
            self.check(
                "affinage : options de rendu directes (sans LLM) écrites et relues",
                r2.ok and opts.get("sections", {}).get("transcript") is False,
                f"post={r2.status} opts={opts}",
            )
            # Le fil rend l'historique seedé ET le bouton « Appliquer cette proposition »
            # (consentement explicite : la proposition est affichée avant application).
            self.page.reload(wait_until="networkidle")
            btn = self.page.locator(".refine-proposal-btn")
            self.check(
                "affinage : proposition affichée + bouton « Appliquer cette proposition »",
                btn.count() == 1 and "condensée" in self.page.locator("#refine-thread").inner_text(),
            )
            self.shot("19_refine_chat")
        except Exception as exc:  # noqa: BLE001
            self.check("panneau d'affinage", False, str(exc)[:120])

    def ux_friendliness(self) -> None:
        # Convivialité « live » : une URL inexistante rend une page d'erreur FRANÇAISE
        # (pas la page Werkzeug brute en anglais) AVEC un chemin de sortie cliquable qui
        # ramène à l'accueil — un cul-de-sac frustrerait l'utilisateur.
        try:
            resp = self.page.goto(f"{self.base}/page-qui-nexiste-pas-xyz", wait_until="networkidle")
            self.shot("20_error_404")
            body = self.page.content()
            status = resp.status if resp else 0
            localized = "introuvable" in body.lower() and "Not Found" not in body
            self.check("404 convivial (français, pas de page brute Werkzeug)", status == 404 and localized, f"status={status}")
            # Le lien « Retour à l'accueil » fonctionne réellement.
            back = self.page.locator('a[href="/"]').first
            if back.count() > 0:
                back.click()
                self.page.wait_for_load_state("networkidle")
                self.check("404 : lien de retour ramène à l'accueil", self.page.url.rstrip("/") == self.base)
            else:
                self.check("404 : lien de retour présent", False, "aucun lien href=/")
        except Exception as exc:  # noqa: BLE001
            self.check("page 404 conviviale", False, str(exc)[:120])

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
    parser.add_argument("--result-job-id", default=None, help="id d'un job terminé seedé → couvre /jobs/<id>/result")
    parser.add_argument("--demo-ids", default=None, help="JSON de seed_demo_dataset.py → plongée par états + parcours par rôle")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        wt = Walkthrough(page, args.base_url, out)
        try:
            wt.admin_password = args.password
            wt.login(args.user, args.password)
            wt.create_job_and_open_wizard()
            wt.config_editor()
            wt.admin_pages()
            wt.admin_crud()
            wt.voice_crud()
            wt.auth_flows()
            wt.ux_friendliness()
            wt.meeting_types_page()
            if args.demo_ids:
                import json as _json
                _ids = _json.loads(Path(args.demo_ids).read_text(encoding="utf-8"))
                wt.states_deep_dive(_ids)
                wt.role_walkthroughs()
            if args.result_job_id:
                wt.job_result_page(args.result_job_id)
                wt.refine_chat_panel(args.result_job_id)
                wt.srt_editor_workshop(args.result_job_id)
        finally:
            browser.close()
        ok = wt.report()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

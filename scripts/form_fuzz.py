#!/usr/bin/env python3
"""Banc « fuzz formulaire » générique — chantier C0.2 (docs/archive/RELEASE_0.2.0.md, vague 0).

Soumet CHAQUE formulaire/API déclaré avec des valeurs aux limites (vide, 1 caractère,
très long, unicode exotique, types incorrects, injections basiques) et vérifie l'ORACLE :

- **jamais de 500** (ni de page Werkzeug/traceback) — un rejet est un 4xx PROPRE ;
- les API JSON répondent du JSON (clé ``error`` sur 4xx), jamais du HTML ;
- le serveur reste VIVANT après chaque salve (« /ready »).

Industrialise la demande mainteneur « tester chaque champ avec données mini, maxi,
incorrectes » — rejouable en CI sur l'instance jetable du walkthrough (mêmes seeds).

    python scripts/form_fuzz.py --base-url http://127.0.0.1:7899 \
        --user admin --password walkthrough-admin-pw [--demo-ids ids.json]

Sortie : un verdict par (formulaire × payload) en échec, un résumé par formulaire,
exit ≠ 0 si au moins un oracle est violé.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import requests

# ── Payloads aux limites (communs à tous les champs texte) ─────────────────────
TEXT_PAYLOADS: list[tuple[str, str]] = [
    ("vide", ""),
    ("un-caractere", "a"),
    ("tres-long-10k", "x" * 10_000),
    ("unicode-exotique", "🦊 ﷽ اختبار עברית ́̀ combiné"),
    ("rtl-override", "abc‮def"),
    ("html-script", "<script>alert(1)</script>"),
    ("html-img-onerror", '"><img src=x onerror=alert(1)>'),
    ("sql-quote", "'; DROP TABLE users; --"),
    ("path-traversal", "../../etc/passwd"),
    ("nul-echappe", "avant\\x00après"),
]
# Pour les champs numériques / typés (JSON)
TYPED_PAYLOADS: list[tuple[str, object]] = [
    ("nombre-texte", "pas-un-nombre"),
    ("nombre-negatif", -1),
    ("nombre-enorme", 10**18),
    ("null", None),
    ("liste-vide", []),
    ("objet-vide", {}),
]


@dataclass
class FormSpec:
    """Un formulaire (POST form-encodé) ou une API JSON à fuzzer."""

    name: str
    url: str                       # cible du POST ({job} substitué)
    kind: str = "form"             # form | json
    base: dict = field(default_factory=dict)   # payload valide de référence
    fuzz_fields: list[str] = field(default_factory=list)  # champs à fuzzer un à un
    typed_fields: list[str] = field(default_factory=list)  # champs aussi soumis aux TYPED
    json_list: bool = False        # l'API attend une LISTE (participants/lexicon)


def build_specs(job_id: str) -> list[FormSpec]:
    """Le périmètre initial du plan (C0.2) : formulaires + API du wizard et de l'admin."""
    return [
        # Auth (sans session : le fuzz du login se fait déconnecté)
        FormSpec("login", "/login", base={"username": "admin", "password": "x"},
                 fuzz_fields=["username", "password"]),
        # Création de job
        FormSpec("jobs/new", "/jobs/new", base={"title": "Fuzz"}, fuzz_fields=["title"]),
        # Wizard — étapes 4/5/6 (API JSON)
        FormSpec("api/context", f"/api/jobs/{job_id}/context", kind="json",
                 base={"title": "T", "meeting_type": "Réunion interne", "summary": "s",
                       "topic": "t", "objective": "o", "notes": "n", "date": "2026-07-04",
                       "language": "fr"},
                 fuzz_fields=["title", "summary", "topic", "objective", "notes", "date",
                              "language", "meeting_type"],
                 typed_fields=["title", "meeting_type"]),
        FormSpec("api/participants", f"/api/jobs/{job_id}/participants", kind="json",
                 base=[{"id": "p1", "name": "N", "function": "", "role": ""}],
                 fuzz_fields=["name", "function", "role"], json_list=True),
        FormSpec("api/lexicon", f"/api/jobs/{job_id}/lexicon", kind="json",
                 base=[{"term": "Terme", "variants": ["v"], "category": "mot suspect",
                        "priority": "normale", "replace_by": ""}],
                 fuzz_fields=["term", "category", "priority", "replace_by"], json_list=True),
        FormSpec("api/lexicon/promote", f"/api/jobs/{job_id}/lexicon/promote", kind="json",
                 base={"term": "Terme", "variants": [], "category": "mot suspect",
                       "priority": "normale", "new_lexicon_name": "Fuzz lexique"},
                 fuzz_fields=["term", "new_lexicon_name", "category", "priority"],
                 typed_fields=["variants"]),
        # Admin — créations (form-encodé)
        FormSpec("admin/users/new", "/admin/users/new",
                 base={"username": "fuzz_user", "display_name": "F", "email": "",
                       "password": "motdepasse-fuzz", "password_confirm": "motdepasse-fuzz",
                       "role": "operator"},
                 fuzz_fields=["username", "display_name", "email", "role"]),
        FormSpec("admin/groups/new", "/admin/groups/new",
                 base={"name": "Groupe fuzz", "description": ""},
                 fuzz_fields=["name", "description"]),
        FormSpec("change_password", "/change-password",
                 base={"current_password": "walkthrough-admin-pw",
                       "new_password": "walkthrough-admin-pw",
                       "confirm_password": "walkthrough-admin-pw"},
                 fuzz_fields=["current_password", "new_password", "confirm_password"]),
        # Planification : création de créneau (API JSON)
        FormSpec("api/schedule/windows", "/api/schedule/windows", kind="json",
                 base={"name": "Fuzz créneau", "days": ["lundi"], "start": "19:00",
                       "end": "23:00", "action": "pause_queue", "enabled": True},
                 fuzz_fields=["name", "start", "end", "action"],
                 typed_fields=["days", "enabled"]),
        # Types de réunion (API JSON de création)
        FormSpec("api/meeting-types", "/api/meeting-types", kind="json",
                 base={"name": "Type fuzz", "badge": "FZ", "banner_text": "B",
                       "palette": {"primary": "112233", "accent": "445566", "light": "EEF2F7"},
                       "fields": [], "detection_hints": []},
                 fuzz_fields=["name", "badge", "banner_text"],
                 typed_fields=["palette", "fields", "detection_hints"]),
    ]


class FuzzRunner:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.user, self.password = user, password
        self.session = requests.Session()
        self.failures: list[str] = []
        self.tested = 0

    def login(self) -> None:
        r = self.session.post(f"{self.base}/login",
                              data={"username": self.user, "password": self.password})
        assert r.status_code == 200, f"login banc impossible: {r.status_code}"

    # ── Oracle ────────────────────────────────────────────────────────────────
    def _verdict(self, spec: FormSpec, label: str, r: requests.Response) -> None:
        self.tested += 1
        problems = []
        if r.status_code >= 500:
            problems.append(f"HTTP {r.status_code}")
        text_head = r.text[:4000]
        if "Traceback (most recent call last)" in text_head or "werkzeug.exceptions" in text_head:
            problems.append("traceback rendu")
        if spec.kind == "json" and r.status_code >= 400:
            ctype = r.headers.get("Content-Type", "")
            if "json" not in ctype:
                problems.append(f"4xx non-JSON ({ctype.split(';')[0]})")
            else:
                try:
                    body = r.json()
                    if r.status_code < 500 and "error" not in body and "message" not in body:
                        problems.append("4xx JSON sans clé error")
                except ValueError:
                    problems.append("4xx JSON invalide")
        if problems:
            self.failures.append(f"[{spec.name}] payload={label} → {', '.join(problems)}")

    def _alive(self) -> bool:
        try:
            return self.session.get(f"{self.base}/ready", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    # ── Exécution d'une spec ──────────────────────────────────────────────────
    def run_spec(self, spec: FormSpec) -> None:
        import copy
        session = requests.Session() if spec.name == "login" else self.session

        def submit(payload) -> requests.Response:
            url = f"{self.base}{spec.url}"
            if spec.kind == "json":
                return session.post(url, json=payload, timeout=30)
            return session.post(url, data=payload, timeout=30)

        # 1. le payload de référence passe (sinon la spec est fausse → signalé)
        r = submit(copy.deepcopy(spec.base))
        if r.status_code >= 500:
            self._verdict(spec, "reference", r)

        # 2. fuzz champ par champ (texte)
        for fld in spec.fuzz_fields:
            for label, value in TEXT_PAYLOADS:
                payload = copy.deepcopy(spec.base)
                target = payload[0] if spec.json_list else payload
                target[fld] = value
                self._verdict(spec, f"{fld}={label}", submit(payload))
        # 3. fuzz typé (JSON uniquement)
        if spec.kind == "json":
            for fld in spec.typed_fields:
                for label, value in TYPED_PAYLOADS:
                    payload = copy.deepcopy(spec.base)
                    target = payload[0] if spec.json_list else payload
                    target[fld] = value
                    self._verdict(spec, f"{fld}={label}", submit(payload))
            # 4. enveloppes dégénérées
            for label, value in [("racine-null", None), ("racine-liste-vide", []),
                                 ("racine-chaine", "x"), ("racine-objet-vide", {})]:
                self._verdict(spec, label, submit(value))
        else:
            # soumission partielle : chaque champ retiré un à un
            for fld in list(spec.base):
                payload = {k: v for k, v in spec.base.items() if k != fld}
                self._verdict(spec, f"sans-{fld}", submit(payload))

        if not self._alive():
            self.failures.append(f"[{spec.name}] LE SERVEUR NE RÉPOND PLUS après la salve")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fuzz des formulaires (C0.2)")
    parser.add_argument("--base-url", default="http://127.0.0.1:7899")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--demo-ids", default=None,
                        help="JSON de seed_demo_dataset.py (fournit un job_id cible)")
    args = parser.parse_args(argv)

    runner = FuzzRunner(args.base_url, args.user, args.password)
    runner.login()

    job_id = ""
    if args.demo_ids:
        ids = json.loads(Path(args.demo_ids).read_text(encoding="utf-8"))
        job_id = ids["job_ids"][0]
    else:
        r = runner.session.post(f"{runner.base}/jobs/new", data={"title": "Job fuzz"},
                                allow_redirects=True)
        job_id = r.url.rstrip("/").split("/")[-1]

    for spec in build_specs(job_id):
        before = len(runner.failures)
        runner.run_spec(spec)
        status = "OK " if len(runner.failures) == before else "ÉCHEC"
        print(f"  [{status}] {spec.name}", flush=True)

    print(f"\n── Fuzz : {runner.tested} soumissions, {len(runner.failures)} violation(s) ──")
    for f in runner.failures[:40]:
        print(f"  ✗ {f}")
    if len(runner.failures) > 40:
        print(f"  … et {len(runner.failures) - 40} autres")
    return 1 if runner.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

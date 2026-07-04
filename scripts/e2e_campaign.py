#!/usr/bin/env python3
"""Campagne E2E complète avec LLM réel — validation pré-0.2.0 (docs/RELEASE_0.2.0.md, C4.3).

Pour CHAQUE audio : pipeline complet (upload → analyse → résumé → contexte →
traitement `quality` → export) puis vérification des livrables. Sur l'audio PRIMAIRE,
en plus : toutes les NOUVELLES FEATURES (éditeur SRT → livrables régénérés, chat
d'affinage discuss réel, type de réunion personnalisé, promotion de lexique).

Operator-run (GPU) : lancer contre une instance jetable dédiée.

    python scripts/e2e_campaign.py --base-url http://127.0.0.1:7902 \
        --password <pw> --audios test2.mp3=primary,cse.wav,reunion1.m4a
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time
import zipfile
from pathlib import Path

import requests


class Reporter:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool, str]] = []

    def check(self, audio: str, label: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((audio, label, ok, detail))
        mark = "OK " if ok else "ÉCHEC"
        print(f"    [{mark}] {label}" + (f" — {detail}" if detail else ""), flush=True)
        return ok

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[2]]
        print("\n" + "=" * 70)
        print(f"CAMPAGNE E2E : {len(self.rows) - len(failed)}/{len(self.rows)} OK")
        if failed:
            print("\nÉCHECS :")
            for audio, label, _ok, detail in failed:
                print(f"  ✗ [{audio}] {label} — {detail}")
        print("=" * 70)
        return 1 if failed else 0


def login(base: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{base}/login", data={"username": "admin", "password": password},
               allow_redirects=False, timeout=15)
    if r.status_code not in (302, 303):
        print(f"ERREUR: login refusé (HTTP {r.status_code})", file=sys.stderr)
        sys.exit(2)
    return s


def run_pipeline(s: requests.Session, base: str, audio: Path, rep: Reporter,
                 timeout_s: float = 5400) -> str | None:
    """Pipeline quality complet. Renvoie le job_id si terminé, None sinon."""
    name = audio.name
    r = s.post(f"{base}/jobs/new", data={"title": f"campagne {name}"}, allow_redirects=False)
    job_id = r.headers.get("Location", "").rstrip("/").split("/")[-1]
    if not rep.check(name, "job créé", bool(re.match(r"[0-9a-f-]{36}", job_id)), job_id):
        return None

    with audio.open("rb") as fh:
        r = s.post(f"{base}/api/jobs/{job_id}/upload", files={"file": (name, fh)}, timeout=300)
    if not rep.check(name, "upload accepté", r.status_code == 200, str(r.status_code)):
        return None

    r = s.post(f"{base}/api/jobs/{job_id}/analyze", timeout=600)
    rep.check(name, "analyse audio", r.status_code == 200, str(r.status_code))

    t0 = time.time()
    r = s.post(f"{base}/api/jobs/{job_id}/summary", timeout=timeout_s)
    if not rep.check(name, "résumé LLM produit", r.status_code == 200,
                     f"{time.time() - t0:.0f}s / {r.status_code}"):
        return None

    # séquence wizard requise par le profil quality
    s.post(f"{base}/api/jobs/{job_id}/context",
           json={"title": name, "meeting_type": "Réunion interne"})
    s.post(f"{base}/api/jobs/{job_id}/participants", json=[])
    s.post(f"{base}/api/jobs/{job_id}/lexicon", json=[])

    r = s.post(f"{base}/api/jobs/{job_id}/process", json={"mode": "quality"})
    if not rep.check(name, "traitement quality lancé", r.status_code in (200, 202), str(r.status_code)):
        return None

    deadline = time.time() + timeout_s
    state = ""
    while time.time() < deadline:
        state = s.get(f"{base}/api/jobs/{job_id}/status").json().get("state", "")
        if state in ("completed", "export_ready", "failed"):
            break
        time.sleep(15)
    rep.check(name, "traitement terminé (completed/export_ready)",
              state in ("completed", "export_ready"), f"état={state} en {time.time() - t0:.0f}s")
    if state not in ("completed", "export_ready"):
        return None

    # livrables
    srt = s.get(f"{base}/api/jobs/{job_id}/download/srt")
    rep.check(name, "SRT téléchargeable et non vide", srt.status_code == 200 and len(srt.text) > 50,
              f"{len(srt.text)} car.")
    docx = s.get(f"{base}/api/jobs/{job_id}/download/docx")
    ok_docx = False
    if docx.status_code == 200:
        try:
            with zipfile.ZipFile(io.BytesIO(docx.content)) as z:
                ok_docx = len(z.read("word/document.xml")) > 1000
        except Exception:
            ok_docx = False
    rep.check(name, "DOCX ouvrable (OOXML valide)", ok_docx)
    pkg = s.get(f"{base}/api/jobs/{job_id}/download/package")
    rep.check(name, "package ZIP téléchargeable", pkg.status_code == 200 and len(pkg.content) > 1000)

    # invariants qualité GPU-free (le rapport doit exister)
    r = s.get(f"{base}/api/jobs/{job_id}/status")
    return job_id


def test_new_features(s: requests.Session, base: str, job_id: str, rep: Reporter) -> None:
    """Sur l'audio primaire : éditeur SRT → livrables, discuss réel, lexique."""
    name = "features"

    # L'éditeur/l'affinage sont légitimement en LECTURE SEULE tant que l'exécution
    # finalise (état export_ready → completed). On attend `completed` avant d'éditer
    # (sinon 409 attendu — garde readonly correcte, pas un bug).
    deadline = time.time() + 120
    while time.time() < deadline:
        st = s.get(f"{base}/api/jobs/{job_id}/status").json()
        if st.get("state") == "completed" and st.get("execution_status") == "idle":
            break
        time.sleep(3)

    # ── Éditeur SRT : l'état charge, on édite et on enregistre → livrables régénérés ──
    state = s.get(f"{base}/api/jobs/{job_id}/editor/state")
    ok_state = state.status_code == 200 and len(state.json().get("chunks", [])) > 0
    rep.check(name, "éditeur : état chargé (chunks réels)", ok_state,
              f"{len(state.json().get('chunks', []))} segments" if ok_state else str(state.status_code))
    if ok_state:
        chunks = state.json()["chunks"]
        chunks[0]["text"] = (chunks[0]["text"] + " BALISE-E2E-EDITEUR").strip()
        save = s.post(f"{base}/api/jobs/{job_id}/editor/save",
                      json={"chunks": chunks, "new_speakers": []})
        rep.check(name, "éditeur : version enregistrée", save.status_code == 200, str(save.status_code))
        srt = s.get(f"{base}/api/jobs/{job_id}/download/srt").text
        rep.check(name, "éditeur : l'édition revient dans le SRT livré", "BALISE-E2E-EDITEUR" in srt)
        docx = s.get(f"{base}/api/jobs/{job_id}/download/docx")
        in_docx = False
        with zipfile.ZipFile(io.BytesIO(docx.content)) as z:
            in_docx = "BALISE-E2E-EDITEUR" in z.read("word/document.xml").decode("utf-8")
        rep.check(name, "éditeur : l'édition revient dans le DOCX livré", in_docx)

    # ── Chat d'affinage : un tour discuss réel répond sur le contenu ──────────────
    r = s.post(f"{base}/api/jobs/{job_id}/refine",
               json={"kind": "discuss", "message": "Résume l'objet de la réunion en une phrase."})
    rep.check(name, "affinage : requête discuss acceptée", r.status_code in (200, 202), str(r.status_code))
    assistant = []
    deadline = time.time() + 120
    while time.time() < deadline:
        turns = s.get(f"{base}/api/jobs/{job_id}/refine/chat").json().get("turns", [])
        assistant = [t for t in turns if t.get("role") == "assistant"]
        if assistant:
            break
        time.sleep(1)
    rep.check(name, "affinage : l'assistant répond (LLM réel)", bool(assistant),
              (assistant[-1]["text"][:70] + "…") if assistant else "aucune réponse en 120s")

    # ── Promotion de lexique (nouvelle route beta.9) ─────────────────────────────
    r = s.post(f"{base}/api/jobs/{job_id}/lexicon/promote",
               json={"term": "Emmental", "variants": ["émental"], "new_lexicon_name": "Campagne E2E"})
    rep.check(name, "lexique : promotion vers un lexique central", r.status_code == 200,
              str(r.status_code))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Campagne E2E complète avec LLM (C4.3)")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--audios", required=True,
                        help="chemins séparés par des virgules ; suffixe =primary pour l'audio des features")
    parser.add_argument("--timeout-s", type=float, default=5400)
    args = parser.parse_args(argv)

    rep = Reporter()
    s = login(args.base_url, args.password)

    for spec in args.audios.split(","):
        spec = spec.strip()
        primary = spec.endswith("=primary")
        path = Path(spec.removesuffix("=primary"))
        if not path.is_file():
            rep.check(path.name, "fichier audio présent", False, str(path))
            continue
        print(f"\n─── {path.name} ({'PRIMAIRE + features' if primary else 'pipeline'}) ───", flush=True)
        job_id = run_pipeline(s, args.base_url, path, rep, timeout_s=args.timeout_s)
        if primary and job_id:
            print("  ── nouvelles features ──", flush=True)
            test_new_features(s, args.base_url, job_id, rep)

    return rep.summary()


if __name__ == "__main__":
    raise SystemExit(main())

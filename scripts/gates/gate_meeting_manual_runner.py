#!/usr/bin/env python3
"""Runner MANUEL de sessions de réunion — éprouve la chaîne intention→bot→job (vague 3).

Joue le rôle du futur meeting-runner (vague 4) À LA MAIN : heartbeat, claim d'UNE session
due, lancement du bot via `scripts/bot.sh` (le contrat d'exécution éprouvé), relais des
événements, POST du résultat (codes 0/1/2/3). C'est le gate du DoD vague 3 : « parcours
complet en immédiat via le runner manuel sur Jitsi réel ».

    python scripts/gates/gate_meeting_manual_runner.py \\
        --portal http://127.0.0.1:7870 --token-file jeton_runner.txt \\
        --platforms jitsi [--once] [--poll-s 15]

Le jeton doit appartenir à un compte listé dans `connectors.meetings.runner_usernames`
(permission OPERATE_MEETING_RUNNER — nominative). Le bot pousse lui-même l'audio + le
manifeste vers la façade avec `job_id` (rattachement D4) via les variables d'environnement
transmises ci-dessous.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _post(portal: str, token: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        portal.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def run_session(portal: str, token: str, intent: dict, runner: str) -> int:
    sid = intent["session_id"]
    print(f"→ session {sid} : {intent['provider']} « {intent['meeting_title']} » "
          f"(tentative {intent['attempt']})")
    _post(portal, token, f"/v1/meetings/{sid}/events", {"runner": runner, "event": "joining"})
    env = os.environ.copy()
    env.update({
        "TRANSCRIA_URL": portal,
        "BOT_LANGUAGE": intent.get("language") or "fr",
        # Rattachement D4 : le bot pousse audio+manifeste sur CE job (façade, part job_id).
        "TRANSCRIA_JOB_ID": intent["job_id"],
    })
    if intent["provider"] == "jitsi":
        # Le gate Jitsi est aujourd'hui le seul chemin qui ENREGISTRE + INGÈRE (manifeste +
        # rattachement) — le bot de prod capte sans ingérer tant que le relais live→batch
        # n'est pas câblé (vague 5). Hors conteneur : Playwright du venv requis.
        token_file = os.environ.get("TRANSCRIA_TOKEN_FILE", "")
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "gates" / "gate_bot_jitsi.py"),
               intent["meeting_ref"], "--transcribe", portal, "--ingest",
               "--job-id", intent["job_id"], "--language", intent.get("language") or "fr"]
        if token_file:
            cmd += ["--token-file", token_file]
        proc = subprocess.run(cmd, env=env, cwd=REPO_ROOT)
    else:
        proc = subprocess.run(
            [str(REPO_ROOT / "scripts" / "bot.sh"), intent["provider"], intent["meeting_ref"]],
            env=env, cwd=REPO_ROOT)
    exit_code = proc.returncode
    print(f"← bot terminé, code {exit_code}")
    status, body = _post(portal, token, f"/v1/meetings/{sid}/result",
                         {"runner": runner, "exit_code": exit_code,
                          "category": "bot", "message": f"code {exit_code}"})
    print(f"  résultat transmis : HTTP {status} {body}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portal", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--platforms", default="jitsi", help="ids catalogue, séparés par des virgules")
    parser.add_argument("--runner", default="manual-runner")
    parser.add_argument("--poll-s", type=float, default=15.0)
    parser.add_argument("--once", action="store_true", help="un cycle claim puis sortie")
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    # le gate Jitsi lira le MÊME jeton (façade STT + ingestion rattachée)
    os.environ.setdefault("TRANSCRIA_TOKEN_FILE", str(Path(args.token_file).resolve()))
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    print(f"runner manuel « {args.runner} » — plateformes {platforms}")
    while True:
        status, _ = _post(args.portal, token, "/v1/runners/heartbeat",
                          {"runner": args.runner, "capacity": 1, "active": 0,
                           "platforms": platforms, "images": []})
        if status != 200:
            print(f"heartbeat refusé (HTTP {status}) — jeton runner ? fonctionnalité activée ?")
            return 3
        status, body = _post(args.portal, token, "/v1/meetings/claim",
                             {"runner": args.runner, "max": 1})
        sessions = body.get("sessions", [])
        if sessions:
            run_session(args.portal, token, sessions[0], args.runner)
        elif args.once:
            print("aucune session due")
        if args.once:
            return 0
        time.sleep(args.poll_s)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Vérification E2E de la topologie SPLIT GPU — cf. docs/PLAN_TEST_SPLIT_VLLM.md.

Deux niveaux, du moins au plus exigeant :

  1. **Plan de contrôle** (toujours) : le nœud de ressources répond (`/health`,
     `/capabilities` : GPU énumérés + moteurs STT déclarés) et le serveur d'arbitrage
     vLLM sert bien l'alias `arbitrage` (`GET /v1/models`).
  2. **Job son E2E** (si `--audio` fourni) : on pilote la frontale comme un utilisateur —
     login → création → upload → analyse → traitement `quality` → polling jusqu'à l'état
     terminal → téléchargement des livrables (SRT, package ZIP, DOCX). Prouve que la
     frontale délègue bien STT/diar au nœud et l'arbitrage LLM au vLLM distant.

Sortie : 0 si tout passe ; non-zéro au premier échec (message actionnable). Best-effort
sur les étapes optionnelles (analyse), strict sur les invariants (livrables présents).

Exemples :
    python scripts/verify_split_topology.py \
        --web http://localhost:7870 --node http://localhost:8002 --arbitrage http://localhost:8080
    python scripts/verify_split_topology.py --audio tests/test2.mp3 \
        --username admin --password "$ADMIN_PASSWORD" --api-key "$TRANSCRIA_INFERENCE_API_KEY"

Variables d'env reconnues (repli si l'argument n'est pas donné) :
    TRANSCRIA_INFERENCE_API_KEY, TRANSCRIA_ADMIN_USER, TRANSCRIA_ADMIN_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

import requests

# États terminaux du pipeline (cf. JobState / _REPROCESSABLE_STATES).
_SUCCESS_STATES = {"completed", "quality_checked", "export_ready"}
_FAILURE_STATES = {"failed", "cancelled"}


def _log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


def _fail(stage: str, msg: str) -> NoReturn:
    print(f"[{stage}] ÉCHEC : {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ── Niveau 1 : plan de contrôle ──────────────────────────────────────────────


def check_resource_node(node_url: str, api_key: str | None) -> None:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        h = requests.get(f"{node_url}/health", timeout=10)
    except requests.RequestException as exc:
        _fail("node", f"/health injoignable sur {node_url} ({exc})")
    if h.status_code != 200:
        _fail("node", f"/health a répondu {h.status_code} (attendu 200)")
    _log("node", "/health OK")

    try:
        c = requests.get(f"{node_url}/capabilities", headers=headers, timeout=15)
    except requests.RequestException as exc:
        _fail("node", f"/capabilities injoignable ({exc})")
    if c.status_code != 200:
        _fail("node", f"/capabilities a répondu {c.status_code} (clé API requise ? --api-key)")
    caps = c.json()
    gpus = caps.get("gpus") or caps.get("devices") or []
    engines = caps.get("stt_engines", [])
    _log("node", f"/capabilities OK — {len(gpus)} GPU(s), {len(engines)} moteur(s) STT déclaré(s)")
    if not gpus:
        _log("node", "⚠ aucun GPU énuméré par le nœud (vérifier l'accès CDI au conteneur)")


def check_arbitrage(arbitrage_url: str, expected_alias: str) -> None:
    try:
        r = requests.get(f"{arbitrage_url}/v1/models", timeout=15)
    except requests.RequestException as exc:
        _fail("arbitrage", f"/v1/models injoignable sur {arbitrage_url} ({exc}) — vLLM démarré ?")
    if r.status_code != 200:
        _fail("arbitrage", f"/v1/models a répondu {r.status_code}")
    ids = [m.get("id") for m in (r.json().get("data") or [])]
    _log("arbitrage", f"/v1/models OK — modèles servis : {ids}")
    if expected_alias not in ids:
        _fail("arbitrage", f"alias '{expected_alias}' absent de /v1/models ({ids}) — vérifier --served-model-name")


# ── Niveau 2 : job son E2E via la frontale ───────────────────────────────────


def run_job(web_url: str, audio: Path, username: str, password: str, mode: str,
            timeout_s: float, poll_s: float) -> None:
    s = requests.Session()

    # 1) Login (CSRF neutralisé côté serveur sur les routes mutantes). Un login réussi
    #    REDIRIGE (302/303) et pose un cookie de session ; un 200 = page de login re-rendue.
    r = s.post(f"{web_url}/login", data={"username": username, "password": password},
               allow_redirects=False, timeout=15)
    if r.status_code not in (302, 303):
        _fail("job", f"login refusé (HTTP {r.status_code}) — identifiants ? (--username/--password)")
    if not s.cookies:
        _fail("job", "login sans cookie de session — authentification non établie")
    _log("job", f"login OK (utilisateur {username})")

    # 2) Création du job → job_id depuis la redirection /jobs/<id>.
    r = s.post(f"{web_url}/jobs/new", data={"title": f"verify-split {audio.name}"},
               allow_redirects=False, timeout=15)
    loc = r.headers.get("Location", "")
    job_id = loc.rstrip("/").split("/")[-1] if "/jobs/" in loc else ""
    if not job_id:
        _fail("job", f"création de job : pas de redirection /jobs/<id> (HTTP {r.status_code}, Location={loc!r})")
    _log("job", f"job créé : {job_id}")

    # 3) Upload de l'audio (champ multipart 'file').
    with audio.open("rb") as fh:
        r = s.post(f"{web_url}/api/jobs/{job_id}/upload",
                   files={"file": (audio.name, fh)}, timeout=120)
    if r.status_code != 200:
        _fail("job", f"upload : HTTP {r.status_code} — {r.text[:200]}")
    _log("job", f"upload OK ({audio.name})")

    # 4) Analyse (diagnostic audio) → état ANALYZED.
    r = s.post(f"{web_url}/api/jobs/{job_id}/analyze", timeout=600)
    _log("job", f"analyse → HTTP {r.status_code}")

    # 5) Résumé rapide. Selon le mode, la réponse est SYNCHRONE ou ENFILÉE :
    #    - rôle `web`/split : enfilé sur le worker → 202 immédiat, on poll `summary_done` ;
    #    - rôle `all`/all-in-one : traité en ligne → la réponse n'arrive qu'à la FIN du STT
    #      + diarisation + LLM (plusieurs minutes avec une LLM lente). D'où `timeout=timeout_s`
    #      (et non une valeur fixe trop courte) : c'est le plafond du job, pas une attente fixe.
    r = s.post(f"{web_url}/api/jobs/{job_id}/summary", timeout=timeout_s)
    if r.status_code not in (200, 202):  # 200 = synchrone (all-in-one), 202 = enfilé (split)
        _fail("job", f"summary : HTTP {r.status_code} — {r.text[:200]}")
    _log("job", f"résumé → HTTP {r.status_code} — polling jusqu'à summary_done…")
    _poll_for(s, web_url, job_id, want={"summary_done"}, timeout_s=timeout_s, poll_s=poll_s, label="summary")

    # 6) Étapes wizard minimales (frontale-local) → état processable. `context` exige
    #    SUMMARY_DONE ; `lexicon` fait avancer PARTICIPANTS_DONE → READY_TO_PROCESS.
    for step, payload in (("context", {}), ("participants", []), ("lexicon", [])):
        r = s.post(f"{web_url}/api/jobs/{job_id}/{step}", json=payload, timeout=60)
        if r.status_code != 200:
            _fail("job", f"{step} : HTTP {r.status_code} — {r.text[:200]}")
        _log("job", f"{step} OK")

    # 7) Traitement complet (STT + diar + correction/relecture/qualité LLM). Synchrone en
    #    all-in-one, enfilé en split → même garde de timeout que le résumé (cf. étape 5).
    r = s.post(f"{web_url}/api/jobs/{job_id}/process", json={"mode": mode}, timeout=timeout_s)
    if r.status_code not in (200, 202):  # 200 = synchrone (all-in-one), 202 = enfilé (split)
        _fail("job", f"process(mode={mode}) : HTTP {r.status_code} — {r.text[:300]}")
    _log("job", f"traitement {mode} accepté (HTTP {r.status_code}) — polling…")
    final = _poll_for(s, web_url, job_id, want=_SUCCESS_STATES, timeout_s=timeout_s, poll_s=poll_s, label="process")
    _log("job", f"✅ traitement terminé (état={final})")
    _check_deliverables(s, web_url, job_id)


def _poll_for(s: requests.Session, web_url: str, job_id: str, *, want: set[str],
              timeout_s: float, poll_s: float, label: str) -> str:
    """Poll GET /status jusqu'à atteindre un état de `want` ; échoue sur un état terminal
    d'échec ou au timeout. Retourne l'état atteint."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        r = s.get(f"{web_url}/api/jobs/{job_id}/status", timeout=15)
        if r.status_code != 200:
            _fail("job", f"[{label}] status : HTTP {r.status_code}")
        st = r.json()
        state = st.get("state", "")
        if state != last:
            _log("job", f"[{label}] état={state} exec={st.get('execution_status')} "
                        f"progress={(st.get('progress') or {}).get('percent', '?')}")
            last = state
        if state in want:
            return state
        if state in _FAILURE_STATES:
            _fail("job", f"[{label}] échec (état={state}) — voir les logs des services")
        time.sleep(poll_s)
    _fail("job", f"[{label}] timeout après {timeout_s:.0f}s (dernier état={last})")


def _check_deliverables(s: requests.Session, web_url: str, job_id: str) -> None:
    for kind in ("srt", "package", "docx"):
        r = s.get(f"{web_url}/api/jobs/{job_id}/download/{kind}", timeout=60)
        if r.status_code != 200 or not r.content:
            _fail("job", f"livrable '{kind}' indisponible (HTTP {r.status_code}, {len(r.content)} octets)")
        _log("job", f"livrable '{kind}' OK ({len(r.content)} octets)")
    _log("job", "✅ tous les livrables présents (SRT, package, DOCX)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--web", default="http://localhost:7870", help="URL de la frontale web")
    p.add_argument("--node", default="http://localhost:8002", help="URL du nœud de ressources")
    p.add_argument("--arbitrage", default="http://localhost:8080", help="URL du vLLM d'arbitrage")
    p.add_argument("--arbitrage-alias", default="arbitrage", help="alias attendu dans /v1/models")
    p.add_argument("--audio", type=Path, default=None, help="fichier son → déclenche le job E2E complet")
    p.add_argument("--mode", default="quality", choices=("fast", "quality"), help="mode de traitement")
    p.add_argument("--username", default=os.environ.get("TRANSCRIA_ADMIN_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("TRANSCRIA_ADMIN_PASSWORD", ""))
    p.add_argument("--api-key", default=os.environ.get("TRANSCRIA_INFERENCE_API_KEY", ""))
    p.add_argument("--timeout", type=float, default=1800.0, help="timeout du job (s)")
    p.add_argument("--poll-interval", type=float, default=5.0, help="intervalle de polling (s)")
    args = p.parse_args(argv)

    _log("plan", "── Niveau 1 : plan de contrôle ──")
    # Les sondes de plan de contrôle sont propres à la topologie SPLIT. En all-in-one,
    # il n'y a pas de nœud de ressources distinct (:8002) ni forcément de LLM exposée à
    # cette adresse → passer `--node ""` / `--arbitrage ""` les saute proprement et le
    # script se réduit au job E2E (le même script valide donc les trois topologies).
    if args.node:
        check_resource_node(args.node.rstrip("/"), args.api_key or None)
    else:
        _log("plan", "Niveau 1 (nœud de ressources) ignoré — all-in-one (pas de :8002).")
    if args.arbitrage:
        check_arbitrage(args.arbitrage.rstrip("/"), args.arbitrage_alias)
    else:
        _log("plan", "Niveau 1 (LLM d'arbitrage) ignoré — endpoint non sondé en direct.")

    if not args.audio:
        _log("plan", "Niveau 2 (job son) ignoré — fournir --audio pour le test E2E complet.")
        _log("plan", "✅ plan de contrôle OK.")
        return 0

    if not args.audio.is_file():
        _fail("job", f"fichier audio introuvable : {args.audio}")
    if not args.password:
        _fail("job", "mot de passe admin requis (--password ou $TRANSCRIA_ADMIN_PASSWORD)")

    _log("job", "── Niveau 2 : job son E2E via la frontale ──")
    run_job(args.web.rstrip("/"), args.audio, args.username, args.password,
            args.mode, args.timeout, args.poll_interval)
    _log("plan", "✅ topologie validée de bout en bout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

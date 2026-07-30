"""Utilitaires partagés pour la vérification et l'attente de ports LLM OpenAI-compatible.

« Prêt » ne veut pas dire « port ouvert » : llama.cpp ouvre le port et répond à
`/v1/models` AVANT d'avoir fini de charger le modèle (les complétions renvoient alors
503 « loading model »). Un simple test de port déclarerait donc « prête » une LLM qui
ne sait pas encore générer — et le 1ᵉʳ appel réel (résumé, correction) échouerait. La
disponibilité réelle se prouve par une **complétion minimale** (`generation_confirmed`).
"""
import logging
import time

logger = logging.getLogger(__name__)

# Sonde minimale : déterministe, coût négligeable. `max_tokens` assez large pour qu'un
# modèle « reasoning » (dont les premiers tokens partent dans <think>) ait généré au
# moins un token comptabilisé, sans peser sur la latence.
_PROBE_PROMPT = "ping"
_PROBE_MAX_TOKENS = 16
_PROBE_TIMEOUT = 30


def generation_confirmed(body: dict | None) -> bool:
    """True si une réponse de complétion PROUVE que le modèle est chargé et génère.

    Fonction **pure** (aucune E/S) → entièrement testable. Accepte trois preuves, pour
    couvrir tous les backends OpenAI-compatible ET les modèles « reasoning » :
      - du **texte** non vide (`choices[].text` en complétion, `message.content` en chat) ;
      - du **raisonnement** non vide (`reasoning_content`) : un modèle reasoning dépense
        ses premiers tokens dans `<think>`, séparés là par llama.cpp — son `text` peut
        rester vide à faible `max_tokens` alors qu'il génère réellement ;
      - à défaut, **au moins un token généré** (`usage.completion_tokens >= 1`).

    Sans les 2ᵉ/3ᵉ critères, `max_tokens` petit + modèle reasoning = faux négatif
    éternel : une sonde `text`-only déclarait « non prêt » un serveur sain, jusqu'au
    timeout (incident du 11/06/2026).
    """
    if not isinstance(body, dict):
        return False
    choices = body.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    if str(first.get("text") or "").strip():
        return True
    if str(first.get("reasoning_content") or "").strip():
        return True
    message = first.get("message")
    if isinstance(message, dict) and (
        str(message.get("content") or "").strip()
        or str(message.get("reasoning_content") or "").strip()
    ):
        return True
    usage = body.get("usage")
    if isinstance(usage, dict):
        try:
            if int(usage.get("completion_tokens") or 0) >= 1:
                return True
        except (TypeError, ValueError):
            pass
    return False


def is_port_open(port: int, timeout: int = 5) -> bool:
    """True si un serveur LLM OpenAI-compatible répond ET sait GÉNÉRER sur le port.

    Deux niveaux : (1) `/v1/models` répond 200 avec au moins un modèle ; (2) une
    complétion minimale confirme que le modèle est **chargé et générant**
    (`generation_confirmed`). Un statut non-200 sur la complétion (typiquement 503
    « loading model » pendant le chargement à froid) signifie « pas encore prêt ».
    """
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if r.status_code != 200:
            return False
        data = r.json().get("data") or []
        if not data:
            return False
        model_id = data[0].get("id", "")
        r2 = requests.post(
            f"http://127.0.0.1:{port}/v1/completions",
            json={
                "model": model_id,
                "prompt": _PROBE_PROMPT,
                "max_tokens": _PROBE_MAX_TOKENS,
                "temperature": 0,
            },
            timeout=_PROBE_TIMEOUT,
        )
        if r2.status_code != 200:
            return False
        return generation_confirmed(r2.json())
    except Exception:
        return False


def wait_for_port(port: int, timeout: int = 300) -> bool:
    """Attend que le port soit prêt (modèle **chargé et générant**), jusqu'à *timeout* s.

    Retourne False si le délai est dépassé. La condition d'arrêt est la disponibilité
    de génération (`is_port_open`), pas la seule ouverture du port — sinon on rendrait
    la main pendant le chargement du modèle.
    """
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        if is_port_open(port):
            logger.info("Port %d prêt (modèle générant) après %.0fs", port, time.time() - start)
            return True
        time.sleep(5)
    logger.error("Timeout attente port %d après %ds (modèle non générant)", port, timeout)
    return False


def _process_name(pid: int) -> str:
    """Nom court du processus (`/proc/<pid>/comm`) — vide s'il a déjà disparu."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _listening_pids(port: int) -> list[int]:
    import subprocess

    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=5,
    )
    return [int(p) for p in result.stdout.strip().split("\n") if p.strip().isdigit()]


def kill_port_listeners(port: int, *, log: logging.Logger | None = None) -> bool:
    """Tue les processus qui ÉCOUTENT sur `port` — SIGTERM, délai de grâce, puis SIGKILL
    pour les survivants. Rend True si le port est (ou était) libérable.

    SOURCE UNIQUE (P1.a, audit 2026-07-30) : deux copies identiques vivaient dans
    `vram_manager` et `llm_backend`. Un port configuré nous appartient par CONTRAT —
    son occupant, même inconnu, est donc légitime à évincer — MAIS les démons protégés
    (`NEVER_KILL`, ex. Ollama) ne sont JAMAIS signalés : systemd les relancerait, et
    leur VRAM se libère par déchargement HTTP, pas par kill. Historiquement, cette
    protection n'existait que sur les kills par pattern.
    """
    import os
    import signal

    from transcria.gpu.kill_patterns import NEVER_KILL

    out = log or logger

    def _actionable(pids: list[int]) -> list[int]:
        keep: list[int] = []
        for pid in pids:
            name = _process_name(pid)
            if any(protected in name.lower() for protected in NEVER_KILL):
                out.error(
                    "Port %d occupé par le démon protégé « %s » (PID %d) — jamais tué "
                    "par TranscrIA : libérer son modèle (déchargement HTTP) ou changer "
                    "le port en config.", port, name, pid,
                )
                continue
            keep.append(pid)
        return keep

    try:
        pids = _actionable(_listening_pids(port))
        if not pids:
            return True
        for pid in pids:
            try:
                out.info("SIGTERM → PID %d (LISTEN port %d)", pid, port)
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(3)
        for pid in _actionable(_listening_pids(port)):
            try:
                out.info("SIGKILL → PID %d (LISTEN port %d)", pid, port)
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return True
    except Exception as exc:  # noqa: BLE001 — l'appelant décide, jamais d'exception
        out.warning("Échec kill port %d: %s", port, exc)
        return False

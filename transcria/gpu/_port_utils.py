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

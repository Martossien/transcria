"""Utilitaires partagés pour la vérification et l'attente de ports LLM OpenAI-compatible."""
import logging
import time

logger = logging.getLogger(__name__)


def is_port_open(port: int, timeout: int = 5) -> bool:
    """Retourne True si un serveur LLM OpenAI-compatible répond sur le port donné."""
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if r.status_code != 200:
            return False
        data = r.json()
        if not data.get("data"):
            return False
        model_id = data["data"][0].get("id", "")
        r2 = requests.post(
            f"http://127.0.0.1:{port}/v1/completions",
            json={"model": model_id, "prompt": "Bonjour", "max_tokens": 5, "temperature": 0},
            timeout=30,
        )
        if r2.status_code == 200:
            choices = r2.json().get("choices", [])
            return len(choices) > 0 and len(choices[0].get("text", "")) > 0
        return False
    except Exception:
        return False


def wait_for_port(port: int, timeout: int = 300) -> bool:
    """Attend que le port soit prêt, jusqu'à *timeout* secondes. Retourne False si timeout."""
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        if is_port_open(port):
            logger.info("Port %d répond après %.0fs", port, time.time() - start)
            return True
        time.sleep(5)
    logger.error("Timeout attente port %d après %ds", port, timeout)
    return False

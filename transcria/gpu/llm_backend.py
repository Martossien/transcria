import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from typing import IO

from transcria.gpu._port_utils import is_port_open as _check_port_open

logger = logging.getLogger(__name__)


class LLMBackend(ABC):

    def __init__(self, config: dict, port: int | None = None):
        self.config = config
        services = config.get("services", {})
        self.port = port or services.get("arbitrage_llm_port", services.get("qwen_port", 8080))
        self._pid: int | None = None

    @property
    @abstractmethod
    def backend_type(self) -> str:
        ...

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def ensure_available(self) -> bool:
        ...

    @abstractmethod
    def shutdown(self) -> bool:
        return True

    @staticmethod
    def _http_get_json(url: str, timeout: int = 5) -> dict | None:
        try:
            import requests
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    @staticmethod
    def is_port_open(port: int, timeout: int = 5) -> bool:
        return _check_port_open(port, timeout=timeout)

    @staticmethod
    def _diagnostic_tail(log_path: str | None, n_lines: int = 25) -> str:
        """Renvoie les dernières lignes du log de lancement, pour expliquer une panne.

        Sans ce contexte, un échec de démarrage (binaire introuvable, OOM GPU,
        ``tensor-split`` incompatible…) reste invisible : le serveur sort en
        silence et l'on n'observe qu'un timeout d'attente du port.
        """
        if not log_path or not os.path.isfile(log_path):
            return f"(aucun log de lancement disponible: {log_path or 'sortie non capturée'})"
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"(impossible de lire {log_path}: {exc})"
        tail = "".join(lines[-n_lines:]).strip()
        if not tail:
            return f"(log de lancement vide: {log_path})"
        return f"Dernières lignes de {log_path}:\n{tail}"

    @staticmethod
    def _wait_for_port(
        port: int,
        timeout: int = 300,
        *,
        proc: "subprocess.Popen | None" = None,
        log_path: str | None = None,
    ) -> bool:
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            if LLMBackend.is_port_open(port):
                logger.info("Port %d répond après %.0fs", port, time.time() - start)
                return True
            # Mort précoce du process lancé : inutile d'attendre tout le timeout —
            # on remonte le code de sortie et le log pour expliquer la panne.
            if proc is not None and proc.poll() is not None:
                logger.error(
                    "Le serveur LLM s'est arrêté avant d'ouvrir le port %d "
                    "(code de sortie=%s). %s",
                    port, proc.returncode, LLMBackend._diagnostic_tail(log_path),
                )
                return False
            time.sleep(5)
        logger.error(
            "Timeout attente port %d après %ds — le serveur LLM ne répond pas. %s",
            port, timeout, LLMBackend._diagnostic_tail(log_path),
        )
        return False


def create_llm_backend(config: dict, backend_type: str | None = None) -> LLMBackend:
    if backend_type is None:
        backend_type = _detect_backend_type(config)

    wf = config.get("workflow", {})
    llm = wf.get("arbitration_llm", {})

    if backend_type == "ollama" or "ollama" in backend_type.lower():
        return OllamaLLMBackend(config)
    elif backend_type == "script":
        return ScriptLLMBackend(config)
    elif backend_type == "http":
        services = config.get("services", {})
        port = llm.get("port") or services.get("arbitrage_llm_port", services.get("qwen_port", 8080))
        return HTTPLLMBackend(config, port=port)
    else:
        services = config.get("services", {})
        if services.get("ollama_url"):
            return OllamaLLMBackend(config)
        if services.get("arbitrage_script") and services["arbitrage_script"].strip():
            return ScriptLLMBackend(config)
        return HTTPLLMBackend(config)


def _detect_backend_type(config: dict) -> str:
    services = config.get("services", {})
    if services.get("ollama_url"):
        return "ollama"
    if services.get("arbitrage_script") and services["arbitrage_script"].strip():
        return "script"
    return "http"


class ScriptLLMBackend(LLMBackend):

    backend_type = "script"

    def __init__(self, config: dict, port: int | None = None):
        super().__init__(config, port)
        svc = config.get("services", {})
        self.launch_script: str = svc.get("arbitrage_script", "./scripts/launch_arbitrage.sh")
        self.stop_script: str = svc.get("stop_script", "./scripts/stop_arbitrage_llm.sh")
        # Sortie du script de lancement, capturée pour diagnostiquer les pannes de
        # démarrage (mirroir de la convention du superviseur STT, cf. stt_<name>_<port>.log).
        self.launch_log_path: str = svc.get("arbitrage_log_path") or f"/tmp/arbitrage_llm_{self.port}.log"
        self._launched_by_us = False

    @property
    def model_id(self) -> str:
        return self.config.get("workflow", {}).get("arbitration_llm", {}).get("model_id") or ""

    def is_available(self) -> bool:
        return self.is_port_open(self.port)

    def ensure_available(self) -> bool:
        if self.is_available():
            logger.debug("LLM (script) déjà disponible sur le port %d", self.port)
            self._launched_by_us = False
            return True

        if not os.path.isfile(self.launch_script):
            logger.error("Script de lancement introuvable: %s", self.launch_script)
            return False

        if self.is_port_open(self.port, timeout=2):
            logger.info("Nettoyage du port %d avant lancement", self.port)
            self._kill_port(self.port)
            time.sleep(3)

        logger.info("Lancement LLM via %s (sortie → %s)", self.launch_script, self.launch_log_path)
        log_fh: IO[bytes] | int
        try:
            log_fh = open(self.launch_log_path, "ab")
        except OSError as exc:
            logger.warning(
                "Impossible d'ouvrir le log de lancement %s (%s) — sortie non capturée",
                self.launch_log_path, exc,
            )
            log_fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                ["/bin/bash", self.launch_script],
                stdout=log_fh,
                stderr=log_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._pid = proc.pid
            self._launched_by_us = True
            logger.info("LLM lancé — PID %d, attente port %d...", proc.pid, self.port)
            return self._wait_for_port(
                self.port, timeout=600, proc=proc, log_path=self.launch_log_path,
            )
        except Exception as exc:
            logger.error("Échec lancement LLM: %s", exc)
            return False
        finally:
            if log_fh is not subprocess.DEVNULL:
                try:
                    log_fh.close()  # type: ignore[union-attr]
                except OSError:
                    pass

    def shutdown(self) -> bool:
        if not self._launched_by_us:
            logger.debug("LLM (script) non lancé par nous, pas d'arrêt")
            return True

        logger.info("Arrêt LLM port %d...", self.port)
        if self._pid:
            import os as _os
            try:
                _os.kill(self._pid, 15)
            except Exception:
                pass
            self._pid = None

        import os as _os
        if _os.path.isfile(self.stop_script):
            try:
                subprocess.run(
                    ["/bin/bash", self.stop_script],
                    capture_output=True, text=True, timeout=30,
                )
                logger.info("Script d'arrêt exécuté: %s", self.stop_script)
            except Exception as exc:
                logger.warning("Échec script d'arrêt: %s", exc)

        ok = self._kill_port(self.port)
        self._launched_by_us = False
        return ok

    @staticmethod
    def _kill_port(port: int) -> bool:
        import os as _os
        import signal

        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            if not pids:
                return True
            for pid in pids:
                try:
                    _os.kill(pid, signal.SIGTERM)
                    logger.debug("SIGTERM → PID %d (LISTEN port %d)", pid, port)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(3)
            result2 = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            survivors = [int(p) for p in result2.stdout.strip().split("\n") if p.strip().isdigit()]
            for pid in survivors:
                try:
                    _os.kill(pid, signal.SIGKILL)
                    logger.debug("SIGKILL → PID %d (LISTEN port %d)", pid, port)
                except (ProcessLookupError, PermissionError):
                    pass
            return True
        except Exception as exc:
            logger.warning("Échec kill port %d: %s", port, exc)
            return False


class OllamaLLMBackend(LLMBackend):

    backend_type = "ollama"

    def __init__(self, config: dict, port: int | None = None):
        svc = config.get("services", {})
        ollama_url = svc.get("ollama_url", "http://127.0.0.1:11434")
        if port is None:
            from urllib.parse import urlparse
            parsed = urlparse(ollama_url)
            port = parsed.port or 11434
        super().__init__(config, port)
        self.ollama_url = ollama_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return f"{self.ollama_url}/v1"

    @property
    def model_id(self) -> str:
        return self.config.get("workflow", {}).get("arbitration_llm", {}).get("model_id") or ""

    def is_available(self) -> bool:
        try:
            import requests
            r = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            if r.status_code != 200:
                return False
            models = [m.get("name", "") for m in r.json().get("models", [])]
            base_model = self.model_id.split(":")[0] if ":" in self.model_id else self.model_id
            for m in models:
                if m.startswith(base_model):
                    return True
            return False
        except Exception:
            return False

    def ensure_available(self) -> bool:
        if self.is_available():
            logger.debug("Ollama disponible, modèle %s trouvé", self.model_id)
            return True
        logger.warning("Ollama: modèle %s non trouvé. Lancez 'ollama pull %s'", self.model_id, self.model_id)
        return False

    def shutdown(self) -> bool:
        return True


class HTTPLLMBackend(LLMBackend):

    backend_type = "http"

    @property
    def model_id(self) -> str:
        return self.config.get("workflow", {}).get(
            "arbitration_llm", {}
        ).get("model_id") or ""

    @property
    def base_url(self) -> str:
        return self.config.get("workflow", {}).get(
            "arbitration_llm", {}
        ).get("api_base", f"http://127.0.0.1:{self.port}/v1")

    def is_available(self) -> bool:
        return self.is_port_open(self.port)

    def ensure_available(self) -> bool:
        if self.is_available():
            logger.debug("LLM (http) déjà disponible sur %s", self.base_url)
            return True
        logger.warning("LLM (http): %s non joignable. Démarrez le serveur manuellement.", self.base_url)
        return False

    def shutdown(self) -> bool:
        return True

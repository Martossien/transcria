import contextlib
import logging
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import IO, Any

from transcria.gpu._port_utils import is_port_open as _check_port_open
from transcria.gpu._port_utils import kill_port_listeners
from transcria.gpu.arbitrage_endpoint import (
    ARBITRAGE_BACKENDS,
    CLOUD_BACKEND,
    cloud_view,
    is_cloud_backend,
    ollama_model_name,
    ollama_name_matches,
)
from transcria.gpu.script_guard import ScriptRefuse, safe_script_path

__all__ = ["CloudCredentialsMissing", "CloudLLMBackend", "LLMBackend", "create_llm_backend", "is_cloud_backend"]

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

    def is_loaded(self) -> bool:
        """Le modèle occupe-t-il la VRAM *maintenant* ?

        Distinct de :meth:`is_available` : pour un serveur mono-modèle (llama.cpp, vLLM),
        « joignable » ⇔ « chargé », donc le défaut délègue à ``is_available``. Un démon
        multi-modèle persistant (Ollama) reste joignable modèle DÉCHARGÉ → il surcharge
        cette méthode pour distinguer les deux (sinon la préemption VRAM le croit toujours
        résident et ne libère jamais la carte pour le STT)."""
        return self.is_available()

    def unload(self) -> bool:
        """Libère la VRAM du modèle. Défaut : arrêt complet du serveur (``shutdown``).

        Un démon persistant (Ollama) surcharge : il décharge le modèle SANS tuer le
        service (le démon n'est pas « à nous » — cf. garde dans VRAMManager)."""
        return self.shutdown()

    def measured_vram_mb(self) -> int | None:
        """VRAM RÉELLE occupée par le modèle chargé (Mo), ou None si non mesurable.

        Sert à RECALER l'empreinte calculée au 1ᵉʳ load (la mesure prime). Défaut : None
        (moteur sans API d'introspection VRAM) ; Ollama surcharge via /api/ps."""
        return None

    @staticmethod
    def _http_get_json(url: str, timeout: int = 5, headers: dict[str, str] | None = None) -> dict | None:
        """GET JSON tolérant MAIS diagnostiquable (C2.4 — plus d'échec silencieux).

        L'appelant garde son contrat historique (``None`` = indisponible), mais la
        NATURE de l'échec (timeout / connexion refusée / DNS / statut HTTP / JSON
        invalide) est journalisée — throttlée par (url, nature) pour ne pas inonder
        les logs quand un poll interroge un démon éteint. ``headers`` : en-têtes
        d'authentification (cloud) — jamais journalisés.
        """
        data, err = LLMBackend._http_get_json_result(url, timeout=timeout, headers=headers)
        if err:
            LLMBackend._log_network_error_throttled(url, err)
        return data

    _NETWORK_ERROR_LOGGED: dict[tuple[str, str], float] = {}
    _NETWORK_ERROR_LOG_EVERY_S = 300.0

    @staticmethod
    def _http_get_json_result(
        url: str, timeout: int = 5, headers: dict[str, str] | None = None, auth: Any = None,
    ) -> tuple[dict | None, str | None]:
        """Renvoie (données, None) ou (None, "nature: détail") — typé, testable."""
        try:
            import requests
            from requests import exceptions as rexc

            try:
                # `headers`/`auth` seulement si fournis : les doublures de test historiques
                # (lambda url, timeout) et les sondes locales restent inchangées.
                kwargs: dict[str, Any] = {}
                if headers:
                    kwargs["headers"] = headers
                if auth is not None:
                    kwargs["auth"] = auth
                r = requests.get(url, timeout=timeout, **kwargs)
            except rexc.ConnectTimeout as exc:
                return None, f"timeout de connexion ({timeout}s): {exc}"
            except rexc.ReadTimeout as exc:
                return None, f"timeout de lecture ({timeout}s): {exc}"
            except rexc.ConnectionError as exc:
                detail = str(exc)
                if "Name or service not known" in detail or "getaddrinfo" in detail:
                    return None, f"résolution DNS impossible: {detail[:160]}"
                return None, f"connexion refusée/coupée: {detail[:160]}"
            if r.status_code != 200:
                # Le corps d'erreur d'un fournisseur (clé invalide, modèle retiré…) est le
                # diagnostic n°1 de l'admin : on le garde, court et sur une ligne (les
                # fournisseurs n'y renvoient jamais la clé).
                snippet = " ".join(str(getattr(r, "text", "") or "").split())[:160]
                return None, f"statut HTTP {r.status_code}" + (f": {snippet}" if snippet else "")
            try:
                return r.json(), None
            except ValueError as exc:
                return None, f"réponse non-JSON: {exc}"
        except Exception as exc:  # noqa: BLE001 — jamais de crash sur un poll réseau
            if headers:
                # Avec des en-têtes d'authentification, le texte d'une exception (ex. InvalidHeader)
                # peut contenir la valeur de l'en-tête : on ne journalise que sa nature.
                return None, f"erreur inattendue: {type(exc).__name__}"
            return None, f"erreur inattendue: {exc}"

    @staticmethod
    def _log_network_error_throttled(url: str, err: str) -> None:
        import time

        kind = err.split(":", 1)[0]
        key = (url, kind)
        now = time.monotonic()
        last = LLMBackend._NETWORK_ERROR_LOGGED.get(key, 0.0)
        if now - last >= LLMBackend._NETWORK_ERROR_LOG_EVERY_S:
            LLMBackend._NETWORK_ERROR_LOGGED[key] = now
            logger.warning("Backend LLM injoignable — %s : %s", url, err)

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
            with open(log_path, encoding="utf-8", errors="replace") as fh:
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

    if backend_type == CLOUD_BACKEND:
        return CloudLLMBackend(config)
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
    # `services.backend` explicite fait autorité (ollama|script|http|cloud) ; sinon auto-détection
    # rétro-compatible : ollama_url ⇒ ollama, arbitrage_script ⇒ script, défaut ⇒ http.
    # (Une valeur INCONNUE est refusée par le schéma — jamais rabattue en silence.)
    explicit = str(services.get("backend", "") or "").strip().lower()
    if explicit in ARBITRAGE_BACKENDS:
        return explicit
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

        # S1.6 : ce chemin vient de la CONFIG et sera exécuté par un service root.
        # La garde remplace l'ancien simple `isfile` (racine autorisée, lien symbolique
        # résolu, refus d'un fichier inscriptible par tous).
        try:
            script_verifie = safe_script_path(self.launch_script, self.config)
        except ScriptRefuse as exc:
            logger.error("Script de lancement REFUSÉ : %s", exc)
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
                ["/bin/bash", str(script_verifie)],
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
            with contextlib.suppress(Exception):
                _os.kill(self._pid, 15)
            self._pid = None

        import os as _os
        if _os.path.isfile(self.stop_script):
            try:
                subprocess.run(
                    ["/bin/bash", str(safe_script_path(self.stop_script, self.config))],
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
        """Source unique protégée (`kill_port_listeners`, NEVER_KILL respecté — P1.a) :
        la copie locale historique était identique à celle de `vram_manager`."""
        return kill_port_listeners(port, log=logger)


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
        # Nom de modèle Ollama NATIF (ex. "qwen3:8b") pour l'API /api/* — résolution
        # partagée avec la page Modèles (source unique dans arbitrage_endpoint).
        return ollama_model_name(self.config)

    @staticmethod
    def _name_matches(candidate: str, wanted: str) -> bool:
        return ollama_name_matches(candidate, wanted)

    def _pulled(self) -> bool:
        """Le modèle est-il téléchargé (présent dans /api/tags) ?"""
        data = self._http_get_json(f"{self.ollama_url}/api/tags")
        if not data:
            return False
        return any(self._name_matches(m.get("name", ""), self.model_id) for m in data.get("models", []))

    def is_available(self) -> bool:
        # « Disponible » = démon joignable ET modèle déjà tiré (le pull est fait à l'install).
        return self._pulled()

    def is_loaded(self) -> bool:
        # /api/ps liste les modèles RÉSIDENTS en mémoire avec leur empreinte VRAM. C'est
        # l'autorité pour la préemption : le port 11434 reste ouvert modèle déchargé, donc
        # is_port_open serait un faux positif « résident ».
        data = self._http_get_json(f"{self.ollama_url}/api/ps")
        if not data:
            return False
        for m in data.get("models", []):
            if self._name_matches(m.get("name", ""), self.model_id) and int(m.get("size_vram", 0) or 0) > 0:
                return True
        return False

    def ensure_available(self) -> bool:
        # On ne PULL jamais au runtime (plusieurs Go pendant un job) : le pull est fait à
        # l'install. Ici on garantit seulement que le modèle est chargé en VRAM via une
        # requête vide (Ollama charge alors le modèle, keep_alive par défaut = 5 min).
        if not self._pulled():
            logger.warning("Ollama : modèle %s non tiré. Lancez 'ollama pull %s'", self.model_id, self.model_id)
            return False
        if self.is_loaded():
            logger.debug("Ollama : modèle %s déjà résident", self.model_id)
            return True
        try:
            import requests
            r = requests.post(
                f"{self.ollama_url}/api/generate",
                json={"model": self.model_id, "prompt": ""},
                timeout=300,
            )
            if r.status_code == 200:
                logger.info("Ollama : modèle %s chargé en VRAM", self.model_id)
                return True
            logger.warning("Ollama : échec chargement %s (HTTP %s)", self.model_id, r.status_code)
            return False
        except Exception as exc:
            logger.warning("Ollama : échec chargement %s : %s", self.model_id, exc)
            return False

    def measured_vram_mb(self) -> int | None:
        # /api/ps donne la VRAM RÉELLE (size_vram) du modèle résident → recalage au 1ᵉʳ load.
        data = self._http_get_json(f"{self.ollama_url}/api/ps")
        if not data:
            return None
        total = sum(
            int(m.get("size_vram", 0) or 0)
            for m in data.get("models", [])
            if self._name_matches(m.get("name", ""), self.model_id)
        )
        return int(total // (1024 * 1024)) if total > 0 else None

    def unload(self) -> bool:
        # Décharge le modèle SANS tuer le démon : requête vide + keep_alive=0
        # (Ollama répond done_reason:"unload"). Idempotent si déjà déchargé.
        try:
            import requests
            r = requests.post(
                f"{self.ollama_url}/api/generate",
                json={"model": self.model_id, "prompt": "", "keep_alive": 0},
                timeout=30,
            )
            if r.status_code == 200:
                logger.info("Ollama : modèle %s déchargé (VRAM libérée)", self.model_id)
                return True
            logger.warning("Ollama : échec déchargement %s (HTTP %s)", self.model_id, r.status_code)
            return False
        except Exception as exc:
            logger.warning("Ollama : échec déchargement %s : %s", self.model_id, exc)
            return False

    def shutdown(self) -> bool:
        # Le démon Ollama est persistant et partagé : on ne l'arrête jamais. Pour libérer
        # la VRAM, utiliser unload().
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


# Ce que la liste des modèles d'un fournisseur PROUVE (mesuré par `CloudLLMBackend.probe_models`).
MODELS_LIST_AUTHENTICATED = "authenticated"   # la liste exige la clé : un 200 prouve la clé
MODELS_LIST_PUBLIC = "public"                 # la liste répond à tout le monde : clé non vérifiée
MODELS_LIST_NONE = "none"                     # pas de liste (404) : rien de vérifiable avant le 1er appel
MODELS_LIST_UNKNOWN = "unknown"               # second appel illisible : rien n'est présumé
_HTTP_STATUS_RE = re.compile(r"statut HTTP (\d{3})")


def _no_auth(request):
    """Auth `requests` qui ne fait RIEN — bloque la lecture de ~/.netrc sans ajouter d'en-tête."""
    return request


@dataclass(frozen=True)
class ModelsProbe:
    """Résultat de ``GET /models`` chez un fournisseur cloud — sans jamais porter la clé."""

    ids: tuple[str, ...] | None = None    # modèles listés (None : pas de liste exploitable)
    error: str | None = None              # panne ou refus (clé, transport, 5xx) ; None = le serveur répond
    status: int = 0                       # statut HTTP de l'appel AVEC clé (0 = aucune réponse HTTP)
    list_kind: str = MODELS_LIST_UNKNOWN  # MODELS_LIST_*

    @property
    def key_proven(self) -> bool:
        return self.error is None and self.list_kind == MODELS_LIST_AUTHENTICATED


class CloudCredentialsMissing(RuntimeError):
    """La variable d'environnement qui porte la clé du fournisseur est absente ou vide."""


class CloudLLMBackend(LLMBackend):
    """LLM d'arbitrage chez un FOURNISSEUR EXTERNE (``services.backend: cloud``).

    TranscrIA est un passe-plat (docs/LLM_CLOUD.md) : ce backend ne lance, n'arrête et ne
    réserve RIEN — il sait seulement joindre le fournisseur (URL de la table des fournisseurs,
    clé lue dans la variable d'environnement du catalogue opencode) et dire s'il répond
    (``GET {base}/models`` avec ``Authorization: Bearer``). Les phases agentiques passent par
    opencode, qui résout la MÊME variable ; les appels directs (``refine_llm.chat_completion``)
    réutilisent ``base_url`` + ``auth_headers()``. La clé n'apparaît jamais dans un journal.
    """

    backend_type = CLOUD_BACKEND

    def __init__(self, config: dict, port: int | None = None):
        super().__init__(config, port)
        self.view = cloud_view(config)

    @property
    def model_id(self) -> str:
        # Référence opencode complète (`<fournisseur>/<modèle>`), comme les autres backends.
        return self.config.get("workflow", {}).get("arbitration_llm", {}).get("model_id") or ""

    @property
    def provider_id(self) -> str:
        return self.view.provider

    @property
    def api_model(self) -> str:
        """Nom du modèle pour l'API directe : premier segment (fournisseur) retiré, RIEN d'autre
        (``openrouter/qwen/qwen3`` → ``qwen/qwen3``)."""
        return self.view.model

    @property
    def base_url(self) -> str:
        return self.view.base_url

    @property
    def api_key_env(self) -> str:
        return self.view.api_key_env

    @property
    def extra_request_fields(self) -> dict:
        """Champs admin fusionnés dans le corps des appels directs (``workflow.llm_cloud.extra_request_fields``)."""
        return dict(self.view.extra_request_fields)

    def api_key(self) -> str | None:
        """Clé lue dans l'environnement — ``None`` si absente OU mal formée.

        Une clé contenant un blanc ou un caractère de contrôle (ex. ``"sk-…\\n…"`` : python-dotenv
        interprète les échappements entre guillemets doubles) ferait lever ``InvalidHeader`` par
        ``requests`` avec la valeur EN CLAIR dans le message : on la refuse ici, en amont de tout
        en-tête, et le message d'erreur ne nomme que la variable.
        """
        env = self.api_key_env
        value = (os.environ.get(env) or "").strip() if env else ""
        if not value:
            return None
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
            logger.error(
                "LLM cloud %s : la variable %s contient un blanc ou un caractère de contrôle — "
                "clé IGNORÉE (vérifier les guillemets/échappements dans le .env)",
                self.provider_id or "?", env,
            )
            return None
        return value

    def auth_headers(self) -> dict[str, str]:
        """En-têtes d'authentification ; lève :class:`CloudCredentialsMissing` (message FR qui
        nomme la variable, jamais la valeur) si la clé manque."""
        key = self.api_key()
        if not key:
            env = self.api_key_env or "(variable inconnue : fournisseur hors table sans workflow.llm_cloud.api_key_env)"
            raise CloudCredentialsMissing(
                f"clé d'API du fournisseur « {self.provider_id or '?'} » absente : "
                f"définir la variable d'environnement {env} dans le .env du service"
            )
        return {"Authorization": f"Bearer {key}"}

    def list_models(self, timeout: int = 10) -> tuple[list[str] | None, str | None]:
        """Identifiants servis par le fournisseur (``GET /models``), ou (None, raison lisible)."""
        if not self.base_url:
            return None, (f"URL du fournisseur « {self.provider_id or '?'} » non résolue "
                          "(hors table : renseigner workflow.arbitration_llm.api_base)")
        try:
            headers = self.auth_headers()
        except CloudCredentialsMissing as exc:
            return None, str(exc)
        data, err = self._http_get_json_result(f"{self.base_url}/models", timeout=timeout, headers=headers)
        if err:
            return None, err
        items = (data or {}).get("data") if isinstance(data, dict) else None
        ids = [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]
        return ids, None

    @staticmethod
    def _status_of(err: str | None) -> int:
        """Statut HTTP porté par une erreur de ``_http_get_json_result`` (0 = pas de réponse)."""
        match = _HTTP_STATUS_RE.search(err or "")
        return int(match.group(1)) if match else 0

    def probe_models(self, timeout: int = 10, *, measure_key_requirement: bool = False) -> ModelsProbe:
        """``GET /models`` AVEC la clé ; avec ``measure_key_requirement`` (doctor), un SECOND appel
        SANS clé si le premier a répondu 200 — pour dire ce que la liste PROUVE, mesuré plutôt que
        supposé par fournisseur (sans mesure, ``list_kind`` reste ``unknown``) :

        - 401/403 avec la clé → le fournisseur refuse la clé (``error``) ;
        - 404 avec la clé → pas de liste (``none``) : le serveur répond, rien d'autre n'est
          vérifiable (opencode, mesuré) ;
        - 200 avec la clé, 401/403 sans → la liste exige la clé (``authenticated``) : clé prouvée ;
        - 200 avec ET sans clé → liste publique (``public``, Ollama cloud/OpenRouter mesurés) :
          la clé n'est contrôlée qu'à l'inférence ;
        - toute autre panne (transport, 5xx) → ``error`` ; second appel illisible → ``unknown``.
        """
        if not self.base_url:
            return ModelsProbe(error=f"URL du fournisseur « {self.provider_id or '?'} » non résolue "
                               "(hors table et hors catalogue : renseigner workflow.arbitration_llm.api_base)")
        try:
            headers = self.auth_headers()
        except CloudCredentialsMissing as exc:
            return ModelsProbe(error=str(exc))
        url = f"{self.base_url}/models"
        data, err = self._http_get_json_result(url, timeout=timeout, headers=headers)
        if err is None or err.startswith("réponse non-JSON"):
            status = 200
        else:
            status = self._status_of(err)
        if err is not None:
            if status == 404 and self.view.qualified:
                # Fournisseur de la TABLE sans liste (opencode, mesuré) : le serveur répond. Hors
                # table, un 404 reste une panne : c'est le « api_base sans /v1 » silencieux.
                return ModelsProbe(status=status, list_kind=MODELS_LIST_NONE)
            return ModelsProbe(error=err, status=status)
        items = (data or {}).get("data") if isinstance(data, dict) else None
        ids = tuple(str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id"))
        if not measure_key_requirement:
            return ModelsProbe(ids=ids, status=status)
        # Sans clé : aucun en-tête Authorization, et `auth=_no_auth` empêche `requests` d'en
        # fabriquer un depuis ~/.netrc (le verdict « liste publique » serait alors faux).
        _data2, err2 = self._http_get_json_result(url, timeout=timeout, headers={"Accept": "application/json"},
                                                  auth=_no_auth)
        if err2 is None:
            kind = MODELS_LIST_PUBLIC
        elif self._status_of(err2) in (401, 403):
            kind = MODELS_LIST_AUTHENTICATED
        else:
            kind = MODELS_LIST_UNKNOWN
        return ModelsProbe(ids=ids, status=status, list_kind=kind)

    def is_available(self) -> bool:
        """Le fournisseur répond-il ? (réseau — JAMAIS depuis une page web).

        Disponible = la liste des modèles répond avec la clé, OU le fournisseur n'a pas de liste
        (404 : opencode, mesuré — exiger un 200 le rendait indisponible à CHAQUE phase). Une clé
        refusée, une panne de transport ou un 5xx = indisponible, journalisé sans la clé.
        """
        probe = self.probe_models()
        if probe.error:
            # Statut seul sur un refus : le corps d'un 401 peut citer la clé, même masquée.
            reason = f"clé refusée (statut HTTP {probe.status})" if probe.status in (401, 403) else probe.error
            logger.error("LLM cloud %s indisponible — %s", self.provider_id or "?", reason)
            return False
        return True

    def ensure_available(self) -> bool:
        # Rien à lancer : « prêt » = le fournisseur répond. Le choix du modèle est vérifié
        # par le doctor (catalogue opencode + fournisseur), pas ici à chaque phase.
        return self.is_available()
    def is_loaded(self) -> bool:
        return False  # aucune VRAM locale, jamais

    def unload(self) -> bool:
        return True

    def shutdown(self) -> bool:
        return True

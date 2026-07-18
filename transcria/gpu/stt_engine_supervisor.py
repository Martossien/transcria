"""Superviseur de moteurs STT servis — cycle de vie CAS A/B/C.

Donne au STT distant l'autonomie VRAM qu'ont déjà le service Flask (in-process) et
la LLM d'arbitrage : à partir d'un moteur *déclaré par l'admin* (placement), décide
s'il faut le réutiliser, le lancer, ou refuser faute de VRAM.
Cf. `docs/SERVICE_RESSOURCES_GPU.md` §2.2 / §4 (plan §12.3).

Non intrusif : le superviseur ne fait qu'allumer ce que l'admin a déclaré, sur le
GPU assigné (ou un repli si `auto_relocate`). Dépendances injectées (sonde santé,
lanceur, planificateur) → testable sans GPU ni subprocess.

  - CAS A : moteur déjà sain (`/v1/models` répond)        → réutilise.
  - CAS B : éteint, VRAM disponible (planificateur)        → lance (place/relocate).
  - CAS C : VRAM saturée                                   → "busy" (503 en amont).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from transcria.gpu.stt_vram_planner import SttVramPlanner
from transcria.gpu.vram_manager import VRAMManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineSpec:
    """Moteur STT déclaré par l'admin (placement)."""

    name: str            # "cohere", "whisper", "qwen3asr", "nemotron", …
    script: str          # scripts/launch_stt_<name>.sh
    gpu: int             # GPU assigné (physique, = STT_GPU)
    gpu_mem: float       # gpu_memory_utilization (fraction du total)
    port: int            # port HTTP
    health_url: str      # ex. http://host:port/v1/models (dérivée de health_path)
    idle_timeout_s: float = 0.0  # > 0 : arrêt après inactivité (idle-stop). 0 = jamais
    # Backend STT servi par ce moteur (piste §2.9, multi-instance) : plusieurs
    # entrées peuvent servir le MÊME backend sous des noms distincts, ex.
    # {name: qwen3asr, …} + {name: qwen3asr-gpu0, backend: qwen3asr, port: 8022}.
    # Défaut = name (comportement historique : appariement par nom exact).
    backend: str = ""
    # Sonde de vie : certains runtimes C++ n'exposent pas /v1/models.
    # health_mode "http_2xx" (défaut) = 200 requis ; "http_any" = TOUTE réponse HTTP
    # prouve la vie (valide pour un serveur mono-modèle qui charge ses poids AVANT de
    # binder le port, ex. parakeet-server). http_any ne doit JAMAIS devenir le défaut :
    # un vLLM bind son port avant d'être prêt.
    health_mode: str = "http_2xx"


@dataclass(frozen=True)
class EnsureResult:
    """Issue de `ensure_ready`.

    status : "ready" (CAS A) | "launched" (CAS B) | "busy" (CAS C) | "error".
    """

    status: str
    gpu_index: int | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.status in ("ready", "launched")


def probe_engine_health(prober: "HealthProber", spec: "EngineSpec") -> bool:
    """Sonde un moteur en honorant son `health_mode` (compat : les probers/fakes
    historiques à un seul argument restent acceptés)."""
    try:
        return prober(spec.health_url, mode=spec.health_mode)  # type: ignore[call-arg]
    except TypeError:
        return prober(spec.health_url)


# health_prober(health_url) -> bool ; launcher(spec, gpu_index) -> bool (lancé & prêt)
# stopper(spec) -> bool (arrêté)
HealthProber = Callable[[str], bool]
Launcher = Callable[[EngineSpec, int], bool]
Stopper = Callable[[EngineSpec], bool]


def build_stt_supervisor(config: dict, *, auto_relocate: bool | None = None) -> "SttEngineSupervisor":
    """Superviseur câblé en production : planificateur (VRAMManager) + sonde HTTP +
    lanceur de script. `auto_relocate` défaut = `resource_node.vram.auto_relocate`.
    """

    rn = config.get("resource_node", {}) or {}
    if auto_relocate is None:
        auto_relocate = bool((rn.get("vram", {}) or {}).get("auto_relocate", False))
    planner = SttVramPlanner.from_vram_manager(VRAMManager(config=config))
    launcher = make_script_launcher(health_prober=http_health_prober)
    return SttEngineSupervisor(planner, http_health_prober, launcher, auto_relocate=auto_relocate)


def engine_specs_from_config(config: dict) -> list[EngineSpec]:
    """Construit les `EngineSpec` depuis le manifeste `resource_node.engines`.

    Chaque entrée : name, script, gpu, port (requis) ; gpu_mem (défaut 0.85),
    host (défaut 127.0.0.1). L'URL de santé est dérivée : http://host:port/v1/models.
    Les entrées invalides sont ignorées avec un warning (non bloquant au démarrage).
    """
    rn = config.get("resource_node", {}) or {}
    specs: list[EngineSpec] = []
    for entry in rn.get("engines", []) or []:
        try:
            port = int(entry["port"])
            host = entry.get("host", "127.0.0.1")
            health_path = str(entry.get("health_path") or "/v1/models")
            if not health_path.startswith("/"):
                health_path = "/" + health_path
            health_mode = str(entry.get("health_mode") or "http_2xx")
            if health_mode not in ("http_2xx", "http_any"):
                logger.warning("[stt-sup] health_mode inconnu %r (moteur %s) — repli http_2xx",
                               health_mode, entry.get("name"))
                health_mode = "http_2xx"
            name = str(entry["name"])
            specs.append(
                EngineSpec(
                    name=name,
                    script=str(entry["script"]),
                    gpu=int(entry["gpu"]),
                    gpu_mem=float(entry.get("gpu_mem", 0.85)),
                    port=port,
                    health_url=f"http://{host}:{port}{health_path}",
                    idle_timeout_s=float(entry.get("idle_timeout_s", 0) or 0),
                    health_mode=health_mode,
                    backend=str(entry.get("backend") or name),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("[stt-sup] entrée resource_node.engines ignorée (%s) : %r", exc, entry)
    return specs


def specs_for_backend(specs: "list[EngineSpec]", backend: str) -> "list[EngineSpec]":
    """Moteurs servant `backend` : nom exact OU champ `backend` (multi-instance §2.9).

    L'appariement historique par nom reste couvert (backend défaut = name)."""
    return [s for s in specs if s.backend == backend or s.name == backend]


class SttEngineSupervisor:
    """Orchestre le cycle de vie A/B/C d'un moteur STT déclaré."""

    def __init__(
        self,
        planner: SttVramPlanner,
        health_prober: HealthProber,
        launcher: Launcher,
        *,
        stopper: Stopper | None = None,
        auto_relocate: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._planner = planner
        self._health = health_prober
        self._launch = launcher
        self._stop = stopper or _default_engine_stop
        self.auto_relocate = bool(auto_relocate)
        self._clock = clock or time.monotonic
        # Dernier usage connu par moteur (mis à jour à chaque ensure_ready réussi).
        # Sert à l'idle-stop ; on ne réclame QUE les moteurs qu'on a nous-mêmes servis.
        self._last_used: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._engine_locks: dict[str, threading.Lock] = {}
        self._engine_locks_guard = threading.Lock()

    def _lock_for(self, engine_name: str):
        """Retourne le verrou local qui sérialise le cycle ensure d'un moteur."""
        with self._engine_locks_guard:
            lock = self._engine_locks.get(engine_name)
            if lock is None:
                lock = threading.Lock()
                self._engine_locks[engine_name] = lock
            return lock

    def status_for(self, spec: EngineSpec) -> dict:
        """État de charge observable pour `/capabilities`.

        Le snapshot est local au process du nœud de ressources. Il ne crée pas de
        verrou si le moteur n'a jamais été assuré, pour garder l'inventaire passif.
        """
        with self._engine_locks_guard:
            lock = self._engine_locks.get(spec.name)
        last_used = self._last_used_for(spec.name)
        return {
            "ensure_in_progress": bool(lock.locked()) if lock is not None else False,
            "last_used_monotonic_s": round(last_used, 3) if last_used is not None else None,
        }

    def _record_used(self, engine_name: str) -> None:
        with self._state_lock:
            self._last_used[engine_name] = self._clock()

    def _last_used_for(self, engine_name: str) -> float | None:
        with self._state_lock:
            return self._last_used.get(engine_name)

    def _forget_used(self, engine_name: str) -> None:
        with self._state_lock:
            self._last_used.pop(engine_name, None)

    def ensure_ready(self, spec: EngineSpec) -> EnsureResult:
        # CAS A — déjà résident et sain.
        if probe_engine_health(self._health, spec):
            self._record_used(spec.name)
            logger.info("[stt-sup] %s CAS A — déjà actif (%s)", spec.name, spec.health_url)
            return EnsureResult("ready", spec.gpu, "cas_a_resident")

        lock = self._lock_for(spec.name)
        if lock.locked():
            logger.info("[stt-sup] %s — ensure déjà en cours, attente du verrou moteur", spec.name)

        with lock:
            logger.debug("[stt-sup] %s — verrou moteur acquis pour ensure", spec.name)
            # Double-check indispensable : un appel concurrent a pu lancer le moteur
            # pendant que celui-ci attendait le verrou.
            if probe_engine_health(self._health, spec):
                self._record_used(spec.name)
                logger.info("[stt-sup] %s CAS A — actif après attente du verrou (%s)", spec.name, spec.health_url)
                return EnsureResult("ready", spec.gpu, "cas_a_after_wait")

            return self._ensure_ready_locked(spec)

    def _ensure_ready_locked(self, spec: EngineSpec) -> EnsureResult:
        # Décision de placement (pré-check VRAM + relocalisation éventuelle).
        decision = self._planner.plan(
            assigned_gpu=spec.gpu,
            gpu_memory_utilization=spec.gpu_mem,
            auto_relocate=self.auto_relocate,
        )
        if decision.status == "busy":
            logger.warning("[stt-sup] %s CAS C — VRAM insuffisante : %s", spec.name, decision.reason)
            return EnsureResult("busy", None, decision.reason)

        # CAS B — lancer sur le GPU décidé (assigné ou relocalisé).
        gpu = decision.gpu_index
        if gpu is None:  # défensif : un placement non-busy a toujours un GPU
            return EnsureResult("error", None, "placement sans gpu")
        logger.info("[stt-sup] %s CAS B — lancement sur GPU %d (%s)", spec.name, gpu, decision.status)
        if not self._launch(spec, gpu):
            logger.error("[stt-sup] %s — échec du lancement sur GPU %d", spec.name, gpu)
            return EnsureResult("error", gpu, "launch_failed")
        self._record_used(spec.name)
        return EnsureResult("launched", gpu, f"cas_b_{decision.status}")

    # ── Idle-stop (minimal, opportuniste) ───────────────────────────────────--

    def stop_engine(self, spec: EngineSpec) -> bool:
        """Arrête un moteur (via le stopper injecté) et oublie son dernier usage."""
        ok = bool(self._stop(spec))
        if ok:
            self._forget_used(spec.name)
            logger.info("[stt-sup] %s arrêté (idle-stop)", spec.name)
        else:
            logger.warning("[stt-sup] %s — échec de l'arrêt (idle-stop)", spec.name)
        return ok

    def reap_idle(self, specs: list[EngineSpec], *, now: float | None = None) -> list[str]:
        """Arrête les moteurs inactifs (déclarés avec idle_timeout_s > 0, up, et dont
        le dernier usage connu dépasse le timeout). Non intrusif : on ne touche QUE
        les moteurs qu'on a nous-mêmes servis (présents dans `_last_used`). Best-effort,
        déclenché opportunément (poll /capabilities, ensure_ready). Retourne les noms arrêtés."""
        now = now if now is not None else self._clock()
        stopped: list[str] = []
        for spec in specs:
            if spec.idle_timeout_s <= 0:
                continue
            last = self._last_used_for(spec.name)
            if last is None or (now - last) < spec.idle_timeout_s:
                continue
            if not probe_engine_health(self._health, spec):  # déjà éteint → rien à faire
                self._forget_used(spec.name)
                continue
            logger.info("[stt-sup] %s inactif depuis %.0fs (> %.0fs) — arrêt",
                        spec.name, now - last, spec.idle_timeout_s)
            if self.stop_engine(spec):
                stopped.append(spec.name)
        return stopped


# ── Adaptateurs de production (coutures injectables pour les tests) ──────────--

def http_health_prober(url: str, *, timeout: float = 3.0, session=None, mode: str = "http_2xx") -> bool:
    """Sonde de vie best-effort, sans exception.

    mode "http_2xx" (défaut) : 200 requis. mode "http_any" : TOUTE réponse HTTP
    (même 404) prouve qu'un serveur écoute — réservé aux runtimes mono-modèle qui
    chargent leurs poids AVANT de binder le port (cf. EngineSpec.health_mode)."""
    import requests

    sess = session or requests
    try:
        status = sess.get(url, timeout=timeout).status_code
        return True if mode == "http_any" else status == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("[stt-sup] sonde %s injoignable : %s", url, exc)
        return False


def _default_engine_run(script: str, env: dict, log_path: str) -> None:
    """Lance le script en processus détaché et persistant (équivalent nohup setsid)."""
    full_env = {**os.environ, **env}
    with open(log_path, "ab") as log:
        subprocess.Popen(  # noqa: S603 — script déclaré par l'admin
            ["bash", script], env=full_env, stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )


def _default_engine_stop(spec: EngineSpec) -> bool:
    """Arrête un moteur via `scripts/stop_stt.sh --port` (arrêt par groupe de process)."""
    from pathlib import Path

    stop_script = Path(__file__).resolve().parents[2] / "scripts" / "stop_stt.sh"
    try:
        r = subprocess.run(  # noqa: S603 — script du dépôt
            ["bash", str(stop_script), "--port", str(spec.port)],
            capture_output=True, timeout=120,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("[stt-sup] stop_stt.sh a échoué pour %s (port %d) : %s", spec.name, spec.port, exc)
        return False


def make_script_launcher(
    *,
    health_prober: HealthProber,
    ready_timeout_s: float = 180.0,
    poll_interval_s: float = 2.0,
    runner: Callable[[str, dict, str], None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    log_dir: str = "/tmp",
) -> Launcher:
    """Fabrique un `launcher` : lance `spec.script` (STT_GPU/STT_PORT surchargés)
    puis attend la readiness via `health_prober`. Retourne True si prêt à temps.

    `runner` et `sleeper` sont injectables → testable sans subprocess réel.
    """
    run = runner or _default_engine_run
    sleep = sleeper or time.sleep

    def launcher(spec: EngineSpec, gpu_index: int) -> bool:
        # STT_GPU_MEM transmis depuis la config (`resource_node.engines[].gpu_mem`) : sans lui,
        # le lanceur retombait sur son défaut 0.85 → le moteur réservait ~0.85×VRAM quelle que
        # soit la valeur configurée (l'admission utilisait gpu_mem, mais PAS le lancement réel).
        env = {"STT_GPU": str(gpu_index), "STT_PORT": str(spec.port), "STT_GPU_MEM": str(spec.gpu_mem)}
        log_path = f"{log_dir}/stt_{spec.name}_{spec.port}.log"
        logger.info("[stt-sup] lancement %s : %s (STT_GPU=%d STT_PORT=%d) → %s",
                    spec.name, spec.script, gpu_index, spec.port, log_path)
        run(spec.script, env, log_path)

        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if probe_engine_health(health_prober, spec):
                logger.info("[stt-sup] %s prêt sur GPU %d (port %d)", spec.name, gpu_index, spec.port)
                return True
            sleep(poll_interval_s)
        logger.error("[stt-sup] %s pas prêt après %.0fs (timeout)", spec.name, ready_timeout_s)
        return False

    return launcher

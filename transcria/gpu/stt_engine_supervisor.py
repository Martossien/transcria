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
import time
from collections.abc import Callable
from dataclasses import dataclass

from transcria.gpu.stt_vram_planner import SttVramPlanner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineSpec:
    """Moteur STT déclaré par l'admin (placement)."""

    name: str            # "cohere", "whisper", …
    script: str          # scripts/launch_stt_<name>.sh
    gpu: int             # GPU assigné (physique, = STT_GPU)
    gpu_mem: float       # gpu_memory_utilization (fraction du total)
    port: int            # port HTTP
    health_url: str      # ex. http://host:port/v1/models


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


# health_prober(health_url) -> bool ; launcher(spec, gpu_index) -> bool (lancé & prêt)
HealthProber = Callable[[str], bool]
Launcher = Callable[[EngineSpec, int], bool]


def build_stt_supervisor(config: dict, *, auto_relocate: bool | None = None) -> "SttEngineSupervisor":
    """Superviseur câblé en production : planificateur (VRAMManager) + sonde HTTP +
    lanceur de script. `auto_relocate` défaut = `resource_node.vram.auto_relocate`.
    """
    from transcria.gpu.stt_vram_planner import SttVramPlanner
    from transcria.gpu.vram_manager import VRAMManager

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
            specs.append(
                EngineSpec(
                    name=str(entry["name"]),
                    script=str(entry["script"]),
                    gpu=int(entry["gpu"]),
                    gpu_mem=float(entry.get("gpu_mem", 0.85)),
                    port=port,
                    health_url=f"http://{host}:{port}/v1/models",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("[stt-sup] entrée resource_node.engines ignorée (%s) : %r", exc, entry)
    return specs


class SttEngineSupervisor:
    """Orchestre le cycle de vie A/B/C d'un moteur STT déclaré."""

    def __init__(
        self,
        planner: SttVramPlanner,
        health_prober: HealthProber,
        launcher: Launcher,
        *,
        auto_relocate: bool = False,
    ) -> None:
        self._planner = planner
        self._health = health_prober
        self._launch = launcher
        self.auto_relocate = bool(auto_relocate)

    def ensure_ready(self, spec: EngineSpec) -> EnsureResult:
        # CAS A — déjà résident et sain.
        if self._health(spec.health_url):
            logger.info("[stt-sup] %s CAS A — déjà actif (%s)", spec.name, spec.health_url)
            return EnsureResult("ready", spec.gpu, "cas_a_resident")

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
        logger.info("[stt-sup] %s CAS B — lancement sur GPU %d (%s)", spec.name, gpu, decision.status)
        if not self._launch(spec, gpu):
            logger.error("[stt-sup] %s — échec du lancement sur GPU %d", spec.name, gpu)
            return EnsureResult("error", gpu, "launch_failed")
        return EnsureResult("launched", gpu, f"cas_b_{decision.status}")


# ── Adaptateurs de production (coutures injectables pour les tests) ──────────--

def http_health_prober(url: str, *, timeout: float = 3.0, session=None) -> bool:
    """True si `url` répond 200 (sonde `/v1/models`). Best-effort, sans exception."""
    import requests

    sess = session or requests
    try:
        return sess.get(url, timeout=timeout).status_code == 200
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
        env = {"STT_GPU": str(gpu_index), "STT_PORT": str(spec.port)}
        log_path = f"{log_dir}/stt_{spec.name}_{spec.port}.log"
        logger.info("[stt-sup] lancement %s : %s (STT_GPU=%d STT_PORT=%d) → %s",
                    spec.name, spec.script, gpu_index, spec.port, log_path)
        run(spec.script, env, log_path)

        deadline = time.monotonic() + ready_timeout_s
        while time.monotonic() < deadline:
            if health_prober(spec.health_url):
                logger.info("[stt-sup] %s prêt sur GPU %d (port %d)", spec.name, gpu_index, spec.port)
                return True
            sleep(poll_interval_s)
        logger.error("[stt-sup] %s pas prêt après %.0fs (timeout)", spec.name, ready_timeout_s)
        return False

    return launcher

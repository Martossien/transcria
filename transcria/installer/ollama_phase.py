"""Phase « Ollama » de l'installateur (backend LLM « facile », scope all-in-one v1).

Miroir de ``opencode_phase`` : orchestration testable (dépendances injectées), effets
réseau/privilégiés délégués à un ``runner``. Rôle : proposer Ollama comme backend
d'arbitrage sans compilation ni token HF —

  1. GARDE : ne rien faire si aucun GPU NVIDIA détecté (``nvidia-smi``) — le driver est
     un prérequis, et on ne délègue JAMAIS son installation au script Ollama.
  2. installer le binaire (``curl … | sh``) sur une VERSION ÉPINGLÉE si absent ;
  3. ``ollama pull`` le modèle du palier (registre Ollama) ;
  4. écrire la config backend (``services.backend=ollama`` + endpoint + modèle) et le
     ``model_id`` opencode (``local/<modèle>``) — la résolution d'endpoint backend-aware
     (:func:`transcria.gpu.opencode_setup.resolve_arbitrage_endpoint`) fait le reste.

Le cycle de vie runtime (démon persistant, chargement/déchargement VRAM) est porté par
:class:`transcria.gpu.llm_backend.OllamaLLMBackend` et la délégation dans ``VRAMManager``.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from transcria.config.yaml_file import set_yaml_file_value

Runner = Callable[..., Any]
ConfirmFn = Callable[[str], bool]
HasCommandFn = Callable[[str], bool]
ProbeFn = Callable[[], bool]
ServeFn = Callable[[], Any]

OLLAMA_INSTALL_URL = "https://ollama.com/install.sh"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Le MODÈLE et le CONTEXTE par palier ne sont PLUS ici : ils viennent du catalogue de
# données `transcria/data/llm_profiles.yaml` via `transcria.config.llm_profiles.select_profile`
# (piloté par le matériel : mono-carte vs multi-GPU spread). Cette phase INSTALLE le modèle
# déjà résolu par l'appelant — aucune donnée modèle hardcodée. Cf. docs/LLM_BACKENDS.md.


def _default_daemon_probe(url: str) -> ProbeFn:
    """Sonde HTTP par défaut : le démon Ollama répond-il sur /api/tags ?"""
    def probe() -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=3):
                return True
        except Exception:
            return False

    return probe


def _daemon_env(plan: "OllamaPlan") -> dict[str, str]:
    """Env du démon Ollama pour ce palier : contexte (KV) + spread multi-GPU.

    `OLLAMA_CONTEXT_LENGTH` fixe le contexte par palier (variable selon la VRAM) ;
    `OLLAMA_SCHED_SPREAD=1` répartit un gros modèle sur plusieurs cartes (multi-GPU)."""
    import os

    env = dict(os.environ)
    if plan.context:
        env["OLLAMA_CONTEXT_LENGTH"] = str(plan.context)
    if plan.sched_spread:
        env["OLLAMA_SCHED_SPREAD"] = "1"
    return env


def _make_default_serve(env: dict[str, str]) -> ServeFn:
    """Fabrique un lanceur `ollama serve` détaché avec l'env du palier (contexte/spread)."""
    def serve() -> None:
        try:
            subprocess.Popen(  # noqa: S603,S607 — commande fixe, démon local
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                start_new_session=True, env=env,
            )
        except OSError:
            pass

    return serve


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class OllamaPlan:
    config_path: Path
    model: str = ""                # résolu par select_profile (catalogue de données)
    context: int = 0               # contexte du palier (OLLAMA_CONTEXT_LENGTH) ; 0 = défaut Ollama
    sched_spread: bool = False      # multi-GPU : répartir le modèle (OLLAMA_SCHED_SPREAD)
    ollama_url: str = DEFAULT_OLLAMA_URL
    gpu_present: bool = False
    interactive: bool = True
    pin_version: str = ""          # OLLAMA_VERSION épinglé (vide = version courante amont)
    install_url: str = OLLAMA_INSTALL_URL


@dataclass
class OllamaResult:
    actions: list[str] = field(default_factory=list)

    def record(self, action: str) -> "OllamaResult":
        self.actions.append(action)
        return self


def _write_backend_config(plan: OllamaPlan) -> None:
    """Écrit la config backend Ollama — source unique consommée au runtime.

    ``services.ollama_model`` = nom NATIF Ollama (``qwen3.5:9b``) ;
    ``model_id`` = ``local/<modèle>`` (format opencode : le runner splitte sur le 1ᵉʳ ``/``
    et envoie le nom nu au backend). Le projet a DEUX endpoints LLM opencode distincts —
    ``workflow.summary_llm`` (étape résumé) ET ``workflow.arbitration_llm`` (correction) —
    on pointe LES DEUX sur Ollama, sinon le résumé lit l'endpoint llama.cpp par défaut (8080)."""
    api_base = f"{plan.ollama_url.rstrip('/')}/v1"
    set_yaml_file_value(plan.config_path, "services.backend", "ollama")
    set_yaml_file_value(plan.config_path, "services.ollama_url", plan.ollama_url)
    set_yaml_file_value(plan.config_path, "services.ollama_model", plan.model)
    if plan.context:
        set_yaml_file_value(plan.config_path, "services.ollama_num_ctx", plan.context)
    set_yaml_file_value(plan.config_path, "services.ollama_sched_spread", plan.sched_spread)
    for block in ("summary_llm", "arbitration_llm"):
        set_yaml_file_value(plan.config_path, f"workflow.{block}.model_id", f"local/{plan.model}")
        set_yaml_file_value(plan.config_path, f"workflow.{block}.api_base", api_base)


def apply_ollama(
    plan: OllamaPlan,
    *,
    console: _ConsoleLike,
    runner: Runner = subprocess.run,
    has_command: HasCommandFn | None = None,
    confirm: ConfirmFn | None = None,
    is_daemon_up: ProbeFn | None = None,
    serve: ServeFn | None = None,
) -> OllamaResult:
    """Installe/configure le backend Ollama (cœur de la nouvelle SECTION install)."""
    result = OllamaResult()
    has_command = has_command or (lambda name: shutil.which(name) is not None)
    confirm = confirm if confirm is not None else (lambda _prompt: False)
    is_daemon_up = is_daemon_up or _default_daemon_probe(plan.ollama_url)
    serve = serve or _make_default_serve(_daemon_env(plan))

    # 1. Garde GPU : Ollama a besoin d'un driver NVIDIA déjà présent. On ne l'installe pas
    #    à sa place (le script Ollama tenterait sinon un cuda-drivers + reboot invasif).
    if not plan.gpu_present:
        console.warn("Aucun GPU NVIDIA détecté (nvidia-smi) — backend Ollama ignoré.")
        return result.record("gpu-absent")

    # 2. Installer le binaire si absent (version épinglée), sinon réutiliser.
    if has_command("ollama"):
        console.ok("Ollama déjà installé — réutilisation.")
        result.record("ollama-present")
    else:
        do_install = True if not plan.interactive else confirm(
            f"Installer Ollama ({plan.install_url}) comme backend LLM « facile » ?"
        )
        if not do_install:
            console.info("Installation Ollama ignorée (choix opérateur).")
            return result.record("install-declined")
        if not has_command("zstd"):
            console.warn("zstd introuvable — l'extraction Ollama peut échouer (installez zstd).")
        version = f" (version épinglée {plan.pin_version})" if plan.pin_version else ""
        console.info(f"Installation d'Ollama{version}…")
        env = {"OLLAMA_VERSION": plan.pin_version} if plan.pin_version else None
        proc = runner(["/bin/sh", "-c", f"curl -fsSL {plan.install_url} | sh"], env=env, check=False)
        if getattr(proc, "returncode", 1) != 0:
            console.error("Échec de l'installation d'Ollama — voir https://ollama.com/download")
            return result.record("install-failed")
        console.ok("Ollama installé.")
        result.record("installed")

    # 3. S'assurer que le démon tourne AVANT le pull. Sur hôte systemd, le script Ollama
    #    l'a démarré ; en conteneur/sans systemd il faut le lancer nous-mêmes (sinon
    #    'ollama pull' échoue avec « connection refused »). C'est aussi la bonne behavior Docker.
    if is_daemon_up():
        result.record("daemon-present")
    else:
        console.info("Démarrage du démon Ollama (ollama serve)…")
        serve()
        deadline = time.time() + 30
        while time.time() < deadline and not is_daemon_up():
            time.sleep(1)
        if is_daemon_up():
            console.ok("Démon Ollama prêt.")
            result.record("daemon-started")
        else:
            console.warn("Démon Ollama injoignable après démarrage — 'ollama pull' peut échouer.")
            result.record("daemon-unreachable")

    # 4. Tirer le modèle du palier (registre Ollama).
    console.info(f"Téléchargement du modèle Ollama « {plan.model} »…")
    proc = runner(["ollama", "pull", plan.model], check=False)
    if getattr(proc, "returncode", 1) != 0:
        console.error(f"Échec 'ollama pull {plan.model}' — le modèle devra être tiré manuellement.")
        result.record("pull-failed")
    else:
        console.ok(f"Modèle « {plan.model} » disponible.")
        result.record("pulled")

    # 5. Écrire la config backend (même en cas d'échec de pull : l'opérateur peut retirer
    #    le modèle plus tard ; la config reste cohérente et pointe le bon endpoint).
    _write_backend_config(plan)
    console.ok(f"Config backend Ollama écrite (endpoint {plan.ollama_url}, modèle local/{plan.model}).")
    return result.record("configured")

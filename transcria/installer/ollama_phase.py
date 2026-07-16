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

from transcria.config.gpu_calibration import apply_gpu_calibration
from transcria.config.yaml_file import set_yaml_file_value
from transcria.install_messages import t

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
    llm_vram_mb: int = 0           # empreinte VRAM estimée (poids modèle + KV) ; 0 = pas de calibration
    gpu_indices: tuple[int, ...] = ()  # cartes GPU visées (ex. (0,) en mono, (0,1) en split)


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
    on pointe LES DEUX sur Ollama, sinon le résumé lit l'endpoint llama.cpp par défaut (8080).

    Calibration VRAM : si ``llm_vram_mb`` est fourni (> 0), on l'écrit directement (estimation
    depuis la taille du modèle Ollama + KV). Sinon on ne touche pas à la calibration (repli —
    le recalage au 1er load gérera l'écart). Important : les empreintes ``TIERS_BY_GB`` de
    ``llm_placement`` sont spécifiques à llama.cpp (GGUF quantizés) et ne correspondent PAS aux
    modèles Ollama (qui ont leurs propres quantizations). On ne les utilise PAS pour Ollama."""
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
    # Calibration VRAM : empreinte fournie par l'appelant (dérivée de la taille Ollama).
    if plan.llm_vram_mb > 0:
        indices = list(plan.gpu_indices) if plan.gpu_indices else [0]
        per_gpu = [plan.llm_vram_mb // len(indices)] * len(indices)
        per_gpu[-1] += plan.llm_vram_mb - sum(per_gpu)
        apply_gpu_calibration(
            plan.config_path,
            vram_mb=plan.llm_vram_mb,
            gpu_indices=indices,
            vram_mb_per_gpu=per_gpu,
        )


def _measure_ollama_vram(plan: "OllamaPlan") -> int:
    """Mesure la taille du modèle Ollama via /api/tags et dérive l'empreinte VRAM.

    Retourne poids (Mo) + KV estimé (contexte du palier, fp16 = 2 octets) + marge 12%.
    Retourne 0 si la mesure échoue (l'appelant garde la calibration existante)."""
    import json
    import urllib.request

    try:
        url = f"{plan.ollama_url.rstrip('/')}/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        for m in data.get("models", []):
            if m.get("name") == plan.model or m.get("model") == plan.model:
                weights_mb = int(m["size"]) // (1024 * 1024)
                # KV : on ne connaît pas l'archi exacte via /api/tags, mais on a
                # context_length et quantization_level. Le KV fp16 à 2 octets est
                # une borne supérieure prudente (Ollama utilise fp16 par défaut).
                # On estime grossièrement : 10% du poids par 64K de contexte (empirique
                # Qwen3.5-9B : ~6,3 Go poids, KV@256K ≈ 2-3 Go → ~40% pour 256K).
                # Formule simple : KV ≈ poids × (contexte / 65536) × 0.1
                # (calibré sur Qwen3.5-9B Q4_K_M : 6288 × (262144/65536) × 0.1 ≈ 2515 Mo)
                ctx = plan.context or int(m.get("details", {}).get("context_length", 32768))
                kv_mb = int(weights_mb * (ctx / 65536) * 0.1)
                total = weights_mb + kv_mb
                # Marge 12% (activations, fragmentation) — cohérent avec llm_footprint.
                return int(total * 1.12)
    except Exception:
        return 0
    return 0


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
        console.warn(t("ol_no_gpu"))
        return result.record("gpu-absent")

    # 2. Installer le binaire si absent (version épinglée), sinon réutiliser.
    if has_command("ollama"):
        console.ok(t("ol_present"))
        result.record("ollama-present")
    else:
        do_install = True if not plan.interactive else confirm(
            f"Installer Ollama ({plan.install_url}) comme backend LLM « facile » ?"
        )
        if not do_install:
            console.info(t("ol_install_skipped"))
            return result.record("install-declined")
        if not has_command("zstd"):
            console.warn(t("ol_zstd_missing"))
        version = f" (version épinglée {plan.pin_version})" if plan.pin_version else ""
        console.info(t("ol_install_start", version=version))
        env = {"OLLAMA_VERSION": plan.pin_version} if plan.pin_version else None
        proc = runner(["/bin/sh", "-c", f"curl -fsSL {plan.install_url} | sh"], env=env, check=False)
        if getattr(proc, "returncode", 1) != 0:
            console.error(t("ol_install_failed"))
            return result.record("install-failed")
        console.ok(t("ol_installed"))
        result.record("installed")

    # 3. S'assurer que le démon tourne AVANT le pull. Sur hôte systemd, le script Ollama
    #    l'a démarré ; en conteneur/sans systemd il faut le lancer nous-mêmes (sinon
    #    'ollama pull' échoue avec « connection refused »). C'est aussi la bonne behavior Docker.
    if is_daemon_up():
        result.record("daemon-present")
    else:
        console.info(t("ol_daemon_start"))
        serve()
        deadline = time.time() + 30
        while time.time() < deadline and not is_daemon_up():
            time.sleep(1)
        if is_daemon_up():
            console.ok(t("ol_daemon_ready"))
            result.record("daemon-started")
        else:
            console.warn(t("ol_daemon_unreachable"))
            result.record("daemon-unreachable")

    # 4. Tirer le modèle du palier (registre Ollama).
    console.info(t("ol_pull_start", model=plan.model))
    proc = runner(["ollama", "pull", plan.model], check=False)
    if getattr(proc, "returncode", 1) != 0:
        console.error(t("ol_pull_failed", model=plan.model))
        result.record("pull-failed")
    else:
        console.ok(t("ol_model_available", model=plan.model))
        result.record("pulled")

    # 4bis. Mesurer la taille du modèle pour dériver l'empreinte VRAM (poids + KV).
    #      Les empreintes TIERS_BY_GB de llm_placement sont spécifiques à llama.cpp (GGUF
    #      quantizés) et ne correspondent PAS aux modèles Ollama. On mesure la taille réelle
    #      via /api/tags et on ajoute le KV calculé au contexte du palier.
    measured_vram_mb = _measure_ollama_vram(plan)
    if measured_vram_mb and measured_vram_mb > 0:
        from dataclasses import replace as _dc_replace
        plan = _dc_replace(plan, llm_vram_mb=measured_vram_mb)
        console.ok(t("ol_vram_measured", mb=measured_vram_mb, ctx=plan.context // 1024))
        result.record("vram-calibrated")

    # 5. Écrire la config backend (même en cas d'échec de pull : l'opérateur peut retirer
    #    le modèle plus tard ; la config reste cohérente et pointe le bon endpoint).
    _write_backend_config(plan)
    console.ok(t("ol_config_written", url=plan.ollama_url, model=plan.model))
    return result.record("configured")

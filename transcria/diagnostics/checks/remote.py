"""Doctor — topologie distante : nœuds d'inférence, STT distant/servi, GPU des nœuds."""
from __future__ import annotations

import os
from typing import Callable

from transcria.diagnostics.checks.common import FAIL, OK, WARN, CheckResult, _t
from transcria.diagnostics.checks.probes import (
    _probe_node_capabilities,
    _probe_node_health,
    _safe_capabilities,
    _safe_health,
)
from transcria.gpu.hardware_advisor import _detect_gpu_totals_mb
from transcria.gpu.stt_instance_planner import (
    DEFAULT_INSTANCE_VRAM_MB,
    DEFAULT_SAFETY_MARGIN_MB,
    llm_reserved_by_gpu,
)
from transcria.ingestion.session_store import MeetingSessionStore
from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    audiocpp_home,
    audiocpp_is_complete,
    resolve_runtimes_dir,
)
from transcria.installer.parakeetcpp_phase import (
    PARAKEETCPP_PINNED_COMMIT,
    parakeetcpp_home,
    parakeetcpp_is_complete,
)


def check_inference_nodes(
    cfg: dict,
    *,
    health: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = _t("chk_inference_nodes")
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, _t("in_local"))

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, WARN, _t("in_no_node", mode=mode),
                           hint=_t("in_no_node_hint"))

    health = health or _probe_node_health
    reachable = [u for u in urls if _safe_health(health, u)]
    fallback = bool(inference.get("fallback_local", True))
    if reachable:
        return CheckResult(name, OK, _t("in_reachable", n=len(reachable), total=len(urls), list=", ".join(reachable)))
    if fallback:
        return CheckResult(name, WARN, _t("in_degraded", total=len(urls)),
                           hint=_t("in_degraded_hint"))
    return CheckResult(name, FAIL, _t("in_down", total=len(urls)),
                       hint=_t("in_down_hint"))

def check_remote_stt_control_plane(cfg: dict) -> CheckResult:
    """Vérifie qu'un STT distant a aussi un nœud de contrôle pour `/engines/ensure`."""
    name = _t("chk_stt_control")
    inference = cfg.get("inference", {}) or {}
    mode = inference.get("mode", "local")
    stt_cfg = (inference.get("stt") or {}) if isinstance(inference.get("stt"), dict) else {}
    backends = (stt_cfg.get("backends") or {}) if isinstance(stt_cfg.get("backends"), dict) else {}
    remote_backends = sorted(
        name
        for name, spec in backends.items()
        if isinstance(spec, dict) and str(spec.get("url") or "").strip()
    )
    if not remote_backends:
        return CheckResult(name, OK, _t("stt_none"))
    if mode not in ("remote", "hybrid"):
        return CheckResult(
            name,
            WARN,
            _t("stt_mode", backends=", ".join(remote_backends), mode=mode),
            hint=_t("stt_mode_hint"),
        )

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if isinstance(n, dict) and n.get("url")] if isinstance(nodes, list) else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        # All-in-one : un backend routé loopback avec un moteur homonyme déclaré dans
        # `resource_node.engines` est assuré EN PROCESS par le gate (pas besoin de nœud
        # de contrôle) — état sain, pas un oubli de config.
        from urllib.parse import urlparse

        declared = {str(e.get("name")) for e in ((cfg.get("resource_node", {}) or {}).get("engines") or [])
                    if isinstance(e, dict)}
        local_served = [
            b for b in remote_backends
            if (urlparse(str(backends[b].get("url"))).hostname in ("127.0.0.1", "localhost", "::1"))
            and b in declared
        ]
        if set(local_served) == set(remote_backends):
            return CheckResult(name, OK, _t("stt_local_served", backends=", ".join(local_served)))
        return CheckResult(
            name,
            WARN,
            _t("stt_no_control", backends=", ".join(remote_backends)),
            hint=_t("stt_no_control_hint"),
        )
    return CheckResult(name, OK, _t("stt_ok", n=len(remote_backends), m=len(urls)))

def check_served_stt_runtimes(cfg: dict) -> CheckResult:
    """Runtimes STT servis déclarés (qwen3asr/voxtralrt/nemotron) : binaire provisionné + commit épinglé.

    Le manifeste `resource_node.engines` peut déclarer un moteur dont le runtime n'a
    jamais été construit (ou l'a été sur un ancien SHA après une montée de version) —
    le lanceur échouerait au premier job. On vérifie ici, avec la commande de reprise."""
    name = _t("chk_served_runtimes")
    engines = ((cfg.get("resource_node", {}) or {}).get("engines") or [])
    declared = {str(e.get("name")) for e in engines if isinstance(e, dict)}

    runtimes_dir = resolve_runtimes_dir()
    known = {
        "qwen3asr": ("audiocpp", lambda: audiocpp_is_complete(audiocpp_home(runtimes_dir), AUDIOCPP_PINNED_COMMIT)),
        # voxtralrt partage le runtime audiocpp mais exige AUSSI son GGUF : un
        # runtime provisionné pour qwen3asr sans le paquet voxtral_realtime
        # donnerait sinon un faux OK (crash au premier lancement du moteur).
        "voxtralrt": ("audiocpp", lambda: (
            audiocpp_is_complete(audiocpp_home(runtimes_dir), AUDIOCPP_PINNED_COMMIT)
            and (audiocpp_home(runtimes_dir) / "src" / "models"
                 / "Voxtral-Mini-4B-Realtime-2602-GGUF").is_dir()
        )),
        "nemotron": ("parakeetcpp", lambda: parakeetcpp_is_complete(parakeetcpp_home(runtimes_dir), PARAKEETCPP_PINNED_COMMIT)),
    }
    concerned = sorted(declared & set(known))
    if not concerned:
        return CheckResult(name, OK, _t("served_rt_none"))
    missing = [e for e in concerned if not known[e][1]()]
    if missing:
        cli_names = ", ".join(known[e][0] for e in missing)
        return CheckResult(
            name, WARN,
            _t("served_rt_missing", engines=", ".join(missing)),
            hint=_t("served_rt_hint", cli=cli_names),
        )
    return CheckResult(name, OK, _t("served_rt_ok", engines=", ".join(concerned)))

def _caps_reports_gpu(capabilities: dict) -> bool:
    """True si `/capabilities` énumère au moins un GPU avec un `free_mb` lisible."""
    for gpu in capabilities.get("gpus", []) or []:
        if not isinstance(gpu, dict):
            continue
        raw = gpu.get("free_mb")
        if raw is None:
            continue
        try:
            int(raw)
        except (TypeError, ValueError):
            continue
        return True
    return False

def check_inference_node_gpus(
    cfg: dict,
    *,
    capabilities_probe: Callable[[str], dict | None] | None = None,
) -> CheckResult:
    """Un nœud de ressources joignable doit énumérer ses GPU (`free_mb`) via `/capabilities`.

    Détecté À L'INSTALLATION : sinon, en prod, les jobs distants défèrent **en silence**
    au pré-vol (`remote_vram_admits` → None faute de données GPU) au lieu d'être
    dispatchés normalement. Mieux vaut le voir au `doctor` que via des jobs qui stagnent.
    """
    name = _t("chk_node_gpus")
    inference = cfg.get("inference", {})
    mode = inference.get("mode", "local")
    if mode == "local":
        return CheckResult(name, OK, _t("ng_local"))

    nodes = inference.get("nodes") or []
    urls = [n.get("url", "") for n in nodes if n.get("url")] if nodes else []
    if not urls and inference.get("url"):
        urls = [inference["url"]]
    if not urls:
        return CheckResult(name, OK, _t("ng_no_node"))

    probe = capabilities_probe or _probe_node_capabilities
    reachable = [(u, caps) for u in urls if (caps := _safe_capabilities(probe, u)) is not None]
    if not reachable:
        return CheckResult(name, OK, _t("ng_no_caps"))

    without_gpu = [u for u, caps in reachable if not _caps_reports_gpu(caps)]
    if without_gpu:
        return CheckResult(
            name, WARN,
            _t("ng_without_gpu", n=len(without_gpu), total=len(reachable), list=", ".join(without_gpu)),
            hint=_t("ng_without_gpu_hint"),
        )
    return CheckResult(name, OK, _t("ng_ok", n=len(reachable)))

def check_stt_instances_vram(
    cfg: dict,
    *,
    gpu_totals_provider: Callable[[], dict[int, int]] | None = None,
) -> CheckResult:
    """Cohérence VRAM des instances STT servies déclarées (lot conseiller matériel).

    Somme, par carte, les instances audiocpp déclarées (~6,5 Go pièce) + la
    réservation LLM déclarée + la marge : un dépassement du total de la carte
    = WARN (le pré-vol refusera ou thrashera en production). Sans GPU détectable
    ou sans instance servie : OK silencieux (rien à vérifier ici)."""
    name = _t("chk_stt_instances_vram")
    provider = gpu_totals_provider or _detect_gpu_totals_mb
    totals = provider()
    engines = ((cfg.get("resource_node") or {}).get("engines") or [])
    served = [e for e in engines if isinstance(e, dict) and any(
        marker in str(e.get("script") or "")
        for marker in ("qwen3asr", "nemotron", "parakeet", "audiocpp"))]
    if not totals or not served:
        return CheckResult(name, OK, _t("stt_inst_nothing"))

    per_gpu: dict[int, int] = {}
    for e in served:
        per_gpu[int(e.get("gpu", 0))] = per_gpu.get(int(e.get("gpu", 0)), 0) + 1
    reserved = llm_reserved_by_gpu(cfg)
    overflows: list[str] = []
    for gpu_index, count in sorted(per_gpu.items()):
        total = totals.get(gpu_index)
        if total is None:
            overflows.append(_t("stt_inst_unknown_gpu", gpu=gpu_index))
            continue
        need = count * DEFAULT_INSTANCE_VRAM_MB + reserved.get(gpu_index, 0) + DEFAULT_SAFETY_MARGIN_MB
        if need > total:
            overflows.append(_t("stt_inst_overflow", gpu=gpu_index, need=need, total=total))
    if overflows:
        return CheckResult(name, WARN, " ; ".join(overflows), hint=_t("stt_inst_hint"))
    return CheckResult(name, OK, _t("stt_inst_ok", n=len(served)))


def check_meeting_scheduling(cfg: dict) -> CheckResult:
    """Réunions planifiées (vague 3) : si activées, la chaîne doit être complète — façade
    active (le bot pousse l'audio par elle), clé de chiffrement présente, un runner vivant.
    Sans runner, la carte « Réunion » est masquée : WARN avec la cause, pas un mystère."""
    name = _t("chk_meetings")
    meetings = ((cfg.get("connectors", {}) or {}).get("meetings", {}) or {})
    if not meetings.get("enabled", False):
        return CheckResult(name, OK, _t("meetings_disabled"))
    problems = []
    if not ((cfg.get("live", {}) or {}).get("facade", {}) or {}).get("enabled", False):
        problems.append(_t("meetings_need_facade"))
    if not (os.environ.get("TRANSCRIA_MEETING_REF_KEY") or "").strip():
        problems.append(_t("meetings_need_key"))
    if problems:
        return CheckResult(name, FAIL, " ; ".join(problems), hint=_t("meetings_hint"))
    try:
        runners = len(MeetingSessionStore.live_runners(max_age_s=86400))
    except Exception:  # noqa: BLE001 — base injoignable : déjà signalé par check_database
        runners = -1
    if runners == 0:
        return CheckResult(name, WARN, _t("meetings_no_runner"), hint=_t("meetings_runner_hint"))
    return CheckResult(name, OK, _t("meetings_ok"))

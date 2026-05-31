"""Profil de concurrence du workflow & observabilité du goulot (C7 / B8).

TranscrIA **ne pilote pas** le multi-concurrence (laissé aux scripts de l'opérateur) :
ce module se contente de **classer**, **mesurer** et **avertir**. Il :

  - **classe** chaque étape en *sérielle* (ressource exclusive, une à la fois :
    diarisation, STT in-process, LLM, CPU) ou *déléguée* (capacité fixée par le backend
    de l'opérateur, ex. STT vLLM `concurrent_safe`) — `build_profile()` ;
  - **mesure** les durées par étape via un enregistreur in-process à fenêtre glissante
    (`StageMetrics`), alimenté par le pipeline au fil des exécutions ;
  - **résume** la part sérielle, l'étape goulot et une **attente estimée** sous charge
    (`profondeur_file × durée_moyenne_du_goulot`) — `summarize_concurrency()`.

Tout est **best-effort et indicatif** : les durées varient (audio, démarrages à froid,
file mouvante) et l'enregistreur est par process (remis à zéro au redémarrage). À
présenter comme un ordre de grandeur, jamais comme une garantie (cf. doc Phase B §10).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

SERIAL = "serial"
DELEGATED = "delegated"

# Classe de base par étape du pipeline (cf. pipeline_service._define_pipeline_steps).
# `transcribe` est résolu dynamiquement (sériel in-process / délégué si STT distant).
_BASE_STAGE_PROFILE: dict[str, dict[str, str]] = {
    "transcribe": {"class": SERIAL, "resource": "gpu"},
    "diarization": {"class": SERIAL, "resource": "gpu"},
    "voice_embed": {"class": SERIAL, "resource": "gpu"},
    "correction": {"class": SERIAL, "resource": "llm"},
    "quality": {"class": SERIAL, "resource": "cpu"},
    "export": {"class": SERIAL, "resource": "cpu"},
}

_DEFAULT_STAGE = {"class": SERIAL, "resource": "gpu"}


def _stt_is_delegated(config: dict) -> bool:
    """Le STT est-il servi à distance (vLLM/SGLang `concurrent_safe`) → délégué ?"""
    from transcria.inference.resource_status import remote_requirements

    return "stt" in remote_requirements(config)


def build_profile(config: dict) -> dict[str, dict[str, str]]:
    """Carte étape → ``{class: serial|delegated, resource: gpu|cpu|llm|stt_backend}``.

    Dérive la classe du STT depuis `concurrent_safe` (via `remote_requirements`) et
    applique les surcharges déclaratives de `workflow.concurrency_profile`.
    """
    profile = {stage: dict(meta) for stage, meta in _BASE_STAGE_PROFILE.items()}
    if _stt_is_delegated(config):
        profile["transcribe"] = {"class": DELEGATED, "resource": "stt_backend"}

    overrides = (config.get("workflow", {}) or {}).get("concurrency_profile", {}) or {}
    for stage, override in overrides.items():
        if not isinstance(override, dict):
            continue
        base = profile.get(stage, dict(_DEFAULT_STAGE))
        klass = override.get("class") or base["class"]
        if klass not in (SERIAL, DELEGATED):
            klass = base["class"]
        resource = override.get("resource") or base["resource"]
        profile[stage] = {"class": klass, "resource": str(resource)}
    return profile


@dataclass(frozen=True)
class StageStat:
    samples: int
    mean_s: float


class StageMetrics:
    """Durées par étape, fenêtre glissante, thread-safe, **par process**.

    Singleton pratique (`get_instance()`) pour que le pipeline alimente le même
    enregistreur que celui lu par la route de statut. Instanciable directement pour
    les tests (isolation).
    """

    _instance: StageMetrics | None = None
    _instance_lock = threading.Lock()

    def __init__(self, window: int = 50) -> None:
        self._window = max(1, int(window))
        self._durations: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> StageMetrics:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def record(self, stage: str, duration_s: float) -> None:
        """Enregistre la durée d'une étape **terminée** (ignore valeurs <0/non finies)."""
        try:
            value = float(duration_s)
        except (TypeError, ValueError):
            return
        if value < 0 or value != value:  # NaN
            return
        with self._lock:
            bucket = self._durations.get(stage)
            if bucket is None:
                bucket = deque(maxlen=self._window)
                self._durations[stage] = bucket
            bucket.append(value)

    def mean(self, stage: str) -> float | None:
        with self._lock:
            bucket = self._durations.get(stage)
            if not bucket:
                return None
            return sum(bucket) / len(bucket)

    def snapshot(self) -> dict[str, StageStat]:
        with self._lock:
            return {
                stage: StageStat(len(bucket), sum(bucket) / len(bucket))
                for stage, bucket in self._durations.items()
                if bucket
            }

    def reset(self) -> None:
        with self._lock:
            self._durations.clear()


def summarize_concurrency(
    config: dict,
    *,
    queue_depth: int = 0,
    metrics: StageMetrics | None = None,
) -> dict:
    """Résumé observabilité : étapes classées, % sériel, goulot, attente estimée.

    `serial_fraction` = part du temps moyen mesuré passée dans des étapes sérielles.
    `bottleneck` = étape **sérielle** la plus longue (elle fixe le débit multi-jobs :
    deux jobs ne peuvent pas partager une même ressource sérielle). `estimated_wait_s`
    = ``queue_depth × durée_moyenne_du_goulot`` (indicatif, None si rien à estimer).
    """
    recorder = metrics if metrics is not None else StageMetrics.get_instance()
    profile = build_profile(config)
    snap = recorder.snapshot()

    stages: list[dict] = []
    serial_total = 0.0
    measured_total = 0.0
    bottleneck: dict | None = None
    for stage, stat in sorted(snap.items()):
        meta = profile.get(stage, _DEFAULT_STAGE)
        measured_total += stat.mean_s
        is_serial = meta["class"] == SERIAL
        if is_serial:
            serial_total += stat.mean_s
            if bottleneck is None or stat.mean_s > bottleneck["mean_s"]:
                bottleneck = {"stage": stage, "mean_s": stat.mean_s, "resource": meta["resource"]}
        stages.append({
            "stage": stage,
            "class": meta["class"],
            "resource": meta["resource"],
            "mean_s": round(stat.mean_s, 2),
            "samples": stat.samples,
        })

    serial_fraction = round(serial_total / measured_total, 3) if measured_total > 0 else None
    queue_depth = max(0, int(queue_depth))
    estimated_wait_s = (
        round(queue_depth * bottleneck["mean_s"], 1) if bottleneck and queue_depth > 0 else None
    )
    return {
        "measured": bool(snap),
        "stages": stages,
        "serial_fraction": serial_fraction,
        "bottleneck": (
            {"stage": bottleneck["stage"], "resource": bottleneck["resource"],
             "mean_s": round(bottleneck["mean_s"], 2)}
            if bottleneck else None
        ),
        "queue_depth": queue_depth,
        "estimated_wait_s": estimated_wait_s,
    }

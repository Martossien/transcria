"""Profils LLM d'arbitrage : chargement du catalogue de données + sélection pilotée matériel.

SOURCE UNIQUE = ``transcria/data/llm_profiles.yaml`` (versionné). Plus aucun palier/modèle
hardcodé dans le code : les 3 moteurs (llama.cpp / Ollama / vLLM) lisent leurs paliers ici.
Surcharge possible via ``config.yaml`` : ``workflow.arbitration_llm.profiles_file``.

La sélection « fait au mieux avec le matériel physiquement présent » :
  - ``select_by: total_vram_mb``           → VRAM cumulée (tensor-split / TP).
  - ``select_by: per_card_then_total``     → mono-GPU : VRAM par-carte ; multi-GPU (≥ seuil) :
                                             VRAM totale (on ACTIVE le multi-GPU, ex. Ollama spread).
On retient le palier le PLUS HAUT dont ``min_vram_mb`` est satisfait par la VRAM de sélection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from transcria.config.yaml_file import load_yaml_file

_DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_profiles.yaml"


@dataclass(frozen=True)
class ProfileChoice:
    engine: str
    tier_id: str
    model: Any                      # str (Ollama/vLLM) ou dict repo/file/dir (llama.cpp)
    context: int
    gpus: int                       # nb de cartes visées (llama.cpp) / 1 par défaut
    tp: int | None = None           # tensor-parallel (vLLM)
    engine_env: dict[str, str] = field(default_factory=dict)   # ex. {OLLAMA_SCHED_SPREAD: "1"}
    footprint: dict[str, Any] = field(default_factory=dict)
    multi_gpu: bool = False


def profiles_path(config: dict | None = None) -> Path:
    """Chemin du fichier de profils : override config > défaut versionné."""
    if config:
        override = (
            (config.get("workflow", {}) or {})
            .get("arbitration_llm", {})
            .get("profiles_file")
        )
        if override:
            return Path(str(override)).expanduser()
    return _DEFAULT_PROFILES_PATH


def load_llm_profiles(config: dict | None = None) -> dict:
    """Charge le catalogue de profils (YAML de données)."""
    data = load_yaml_file(profiles_path(config))
    if not data or "engines" not in data:
        raise ValueError(f"catalogue de profils LLM invalide : {profiles_path(config)}")
    return data


def _engine_tiers(profiles: dict, engine: str) -> tuple[dict, list[dict]]:
    engines = profiles.get("engines", {}) or {}
    spec = engines.get(engine)
    if not spec:
        raise KeyError(f"moteur inconnu dans le catalogue de profils : {engine}")
    tiers = sorted(spec.get("tiers", []), key=lambda t: int(t.get("min_vram_mb", 0)))
    return spec, tiers


def valid_tp(gpu_count: int, valid: list[int]) -> int:
    """Plus grand TP valide (divisibilité des têtes) ≤ nb de GPU ; 1 par défaut."""
    fitting = [v for v in sorted(valid) if v <= max(gpu_count, 1)]
    return fitting[-1] if fitting else 1


def recommend_engine(
    profiles: dict,
    *,
    gpu_count: int,
    per_card_vram_mb: int,
    total_vram_mb: int,
) -> dict:
    """Recommandation de moteur PILOTÉE PAR LES DONNÉES (bloc ``engine_recommendation``
    du catalogue) — C2.1 : l'installeur recommande ET explique, sans jamais imposer.

    Renvoie ``{engine, reason, llamacpp: ProfileChoice|None, ollama: ProfileChoice|None}``.
    """
    rec = profiles.get("engine_recommendation", {}) or {}
    threshold = int(rec.get("prefer_llamacpp_when_per_card_vram_mb_lt", 0))

    kwargs = dict(gpu_count=gpu_count, per_card_vram_mb=per_card_vram_mb,
                  total_vram_mb=total_vram_mb)
    llamacpp = select_profile(profiles, "llamacpp", **kwargs)
    ollama = select_profile(profiles, "ollama", **kwargs)

    prefer_llamacpp = bool(threshold) and per_card_vram_mb < threshold
    engine = "llamacpp" if prefer_llamacpp else "ollama"
    # Repli honnête : si le moteur recommandé n'a AUCUN palier atteignable, l'autre gagne.
    if engine == "llamacpp" and llamacpp is None and ollama is not None:
        engine = "ollama"
    if engine == "ollama" and ollama is None and llamacpp is not None:
        engine = "llamacpp"

    def _model_label(choice: ProfileChoice | None) -> str:
        if choice is None:
            return "aucun modèle (palier insuffisant)"
        model = choice.model
        if isinstance(model, dict):
            return str(model.get("file") or model.get("repo") or "?").replace(".gguf", "")
        return str(model)

    template = rec.get("reason_llamacpp" if engine == "llamacpp" else "reason_ollama", "")
    reason = str(template).format(
        per_card_gb=round(per_card_vram_mb / 1024),
        llamacpp_model=_model_label(llamacpp),
        llamacpp_ctx=(llamacpp.context // 1024) if llamacpp else 0,
        ollama_model=_model_label(ollama),
    ) if template else ""

    return {"engine": engine, "reason": reason, "llamacpp": llamacpp, "ollama": ollama}


def select_profile(
    profiles: dict,
    engine: str,
    *,
    gpu_count: int,
    per_card_vram_mb: int,
    total_vram_mb: int,
) -> ProfileChoice | None:
    """Meilleur palier tenant compte du matériel. None si aucun palier n'est atteint."""
    spec, tiers = _engine_tiers(profiles, engine)
    if not tiers:
        return None

    select_by = spec.get("select_by", "total_vram_mb")
    multi_cfg = spec.get("multi_gpu", {}) or {}
    multi_threshold = int(multi_cfg.get("enable_when_gpus_gte", 2))
    multi_gpu = gpu_count >= multi_threshold

    # VRAM de sélection selon la stratégie du moteur.
    if select_by == "per_card_then_total":
        selection_vram = total_vram_mb if multi_gpu else per_card_vram_mb
    else:  # total_vram_mb (llama.cpp tensor-split, vLLM TP)
        selection_vram = total_vram_mb
        multi_gpu = gpu_count >= 2

    # Palier le plus haut dont le seuil est satisfait.
    chosen = None
    for tier in tiers:
        if selection_vram >= int(tier.get("min_vram_mb", 0)):
            chosen = tier
    if chosen is None:
        return None

    engine_env: dict[str, str] = {}
    if multi_gpu and multi_cfg.get("env"):
        engine_env.update({str(k): str(v) for k, v in multi_cfg["env"].items()})

    tp = None
    if "tp" in spec:
        tp_cfg = spec["tp"] or {}
        tp = int(chosen.get("tp", 1))
        if tp_cfg.get("auto_from_gpu_count"):
            tp = min(tp, valid_tp(gpu_count, list(tp_cfg.get("valid", [1]))))

    return ProfileChoice(
        engine=engine,
        tier_id=str(chosen.get("id", "")),
        model=chosen.get("model"),
        context=int(chosen.get("context", 0)),
        gpus=int(chosen.get("gpus", 1)),
        tp=tp,
        engine_env=engine_env,
        footprint=dict(chosen.get("footprint", {})),
        multi_gpu=multi_gpu,
    )

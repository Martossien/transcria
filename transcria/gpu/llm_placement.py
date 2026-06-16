"""Planification du placement de la LLM d'arbitrage sur les GPU réellement présents.

Pourquoi ce module existe
-------------------------
L'empreinte VRAM d'un modèle = **poids + KV(contexte) + buffers compute (par GPU)**.
Elle n'est pas prédictible par un simple calcul : le KV dépend de l'architecture
(le 35B MoE Gated-Delta a un KV plus petit qu'un 9B dense !) et le compute du nombre
de cartes. On s'appuie donc sur des empreintes **mesurées** par palier
(cf. docs/BENCH_LLM_PALIERS.md) et on vérifie qu'un placement **tient réellement** sur
la topologie de la machine — au lieu de raisonner sur la VRAM *totale*, qui ignore
qu'une carte ne peut héberger qu'une fraction du modèle (« 2× 8 Go = 16 Go » est faux
dès que le profil est mono-GPU ou que le split égal déborde la plus petite carte).

Ce module est **pur** (aucune E/S) : toute lecture nvidia-smi / config / mesure vit
dans ``scripts/plan_llm_placement.py``. La logique est ainsi entièrement testable sans
GPU (cf. ``tests/test_llm_placement.py``, qui rejoue tout l'univers des cartes NVIDIA).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

CTX_192K = 196608
CTX_256K = 262144

# Marge libre exigée par carte APRÈS la LLM. Plancher calé sur le bench
# (docs/BENCH_LLM_PALIERS.md) : palier 24 Go accepté à ~1,8 Go libre, quant Q4_K_M
# REJETÉ pour la prod à 0,2 Go libre (OOM dès qu'un autre process touche la carte).
DEFAULT_SAFETY_MARGIN_MB = 1500

# Seuil de dérive déclaré/mesuré (en %) au-delà duquel on alerte (calibration périmée).
DEFAULT_DRIFT_PCT = 15


@dataclass(frozen=True)
class Tier:
    """Un palier = un modèle retenu, son empreinte mesurée et le placement du profil livré."""

    gb: int
    footprint_mb: int  # empreinte réelle mesurée (poids + KV + compute) AU CONTEXTE du profil
    profile_gpus: int  # nombre de cartes sur lesquelles le profil livré répartit le modèle
    ctx: int
    label: str


# Empreintes mesurées — source : docs/BENCH_LLM_PALIERS.md (Phase A + Phase B, 06/2026).
# `profile_gpus` reflète le placement des profils livrés (scripts/arbitrage_profiles/ ;
# cf. scripts/switch_arbitrage_llm.sh) : 12/16/24 = mono-GPU, 32/48 = 2 cartes, 64 = 3.
TIERS: tuple[Tier, ...] = (
    Tier(12, 10400, 1, CTX_192K, "Qwen3.5-9B Q5_K_M"),
    Tier(16, 12700, 1, CTX_256K, "Qwen3.5-9B Q6_K"),
    Tier(24, 22300, 1, CTX_256K, "Qwen3.6-35B-A3B UD-IQ4_NL_XL"),
    Tier(32, 29200, 2, CTX_192K, "Qwen3.6-27B Q5_K_M"),
    Tier(48, 36000, 2, CTX_256K, "Qwen3.6-35B-A3B UD-Q6_K"),
    Tier(64, 49000, 3, CTX_256K, "Qwen3.6-35B-A3B UD-Q8_K_XL"),
)
TIERS_BY_GB: dict[int, Tier] = {t.gb: t for t in TIERS}


@dataclass(frozen=True)
class Placement:
    """Résultat d'une planification : faisable ou non, avec calibration et avertissements.

    `feasible=False` ⇒ `reason` explique pourquoi (jamais de choix silencieux qui OOM).
    `vram_mb_per_gpu` est une **estimation** (split égal) à raffiner par mesure réelle.
    """

    tier_gb: int
    feasible: bool
    reason: str
    gpu_indices: list[int] = field(default_factory=list)
    vram_mb: int = 0
    vram_mb_per_gpu: list[int] = field(default_factory=list)
    ctx: int = 0
    warnings: list[str] = field(default_factory=list)


def _split_shares(footprint_mb: int, k: int) -> list[int]:
    """Répartit l'empreinte en `k` parts entières dont la somme vaut exactement `footprint_mb`.

    Le reste est posé sur les premières cartes (la carte 0 porte souvent un peu plus :
    KV / tampons). Ce sont des estimations de split égal, raffinées ensuite à la mesure.
    """
    if k <= 0:
        raise ValueError("k doit être positif")
    base, rem = divmod(int(footprint_mb), k)
    return [base + (1 if i < rem else 0) for i in range(k)]


def plan_for_tier(
    tier_gb: int,
    gpu_sizes_mb: list[int],
    *,
    safety_margin_mb: int = DEFAULT_SAFETY_MARGIN_MB,
) -> Placement:
    """Planifie un palier DONNÉ sur une topologie donnée (tailles de cartes en Mio).

    On valide contre ce que fait *réellement* le profil livré :
      - mono-GPU : le profil épingle une carte → on vérifie la plus grande carte.
      - multi-GPU : le profil répartit (`--tensor-split`) sur les `k` premières cartes,
        en parts égales → la contrainte est la plus petite de ces `k` cartes.
    """
    tier = TIERS_BY_GB.get(int(tier_gb))
    if tier is None:
        return Placement(int(tier_gb), False, f"palier inconnu : {tier_gb}")

    sizes = [int(s) for s in gpu_sizes_mb]
    if not sizes:
        return Placement(tier.gb, False, "aucun GPU NVIDIA détecté")
    if any(s <= 0 for s in sizes):
        return Placement(tier.gb, False, f"taille de GPU invalide dans {sizes}")

    footprint = tier.footprint_mb
    k = tier.profile_gpus
    warnings: list[str] = []

    # ── Palier mono-GPU ───────────────────────────────────────────────────────
    if k == 1:
        biggest = max(sizes)
        idx = sizes.index(biggest)
        need = footprint + safety_margin_mb
        if biggest < need:
            return Placement(
                tier.gb,
                False,
                f"le plus gros GPU ({biggest} Mio) ne tient pas {tier.label} : "
                f"{footprint} Mio + {safety_margin_mb} de marge = {need} Mio requis",
            )
        if idx != 0:
            warnings.append(
                f"la plus grande carte est l'index {idx} (pas 0) : posez "
                f"ARBITRAGE_GPU={idx} dans le profil, sinon le modèle ciblera la carte 0."
            )
        if len(sizes) > 1:
            warnings.append(f"{len(sizes) - 1} carte(s) inutilisée(s) par ce palier mono-GPU.")
        return Placement(
            tier.gb,
            True,
            f"mono-GPU sur la carte {idx}",
            gpu_indices=[idx],
            vram_mb=footprint,
            vram_mb_per_gpu=[footprint],
            ctx=tier.ctx,
            warnings=warnings,
        )

    # ── Palier multi-GPU (split sur les k premières cartes) ─────────────────────
    if len(sizes) < k:
        return Placement(
            tier.gb,
            False,
            f"le profil {tier.gb} Go répartit sur {k} cartes, "
            f"{len(sizes)} détectée(s)",
        )
    used = sizes[:k]
    shares = _split_shares(footprint, k)
    for i, (cap, share) in enumerate(zip(used, shares)):
        if cap < share + safety_margin_mb:
            return Placement(
                tier.gb,
                False,
                f"split égal sur {k} cartes : la carte {i} ({cap} Mio) ne tient pas "
                f"sa part ({share} Mio + {safety_margin_mb} de marge = "
                f"{share + safety_margin_mb} Mio)",
            )
    if len(set(used)) > 1:
        warnings.append(
            "cartes hétérogènes : le split égal sous-exploite les grandes cartes ; "
            "un --tensor-split pondéré (profil personnalisé) optimiserait la marge."
        )
    if len(sizes) > k:
        warnings.append(
            f"{len(sizes) - k} carte(s) au-delà des {k} utilisées par ce profil seront ignorées."
        )
    return Placement(
        tier.gb,
        True,
        f"réparti sur {k} cartes",
        gpu_indices=list(range(k)),
        vram_mb=footprint,
        vram_mb_per_gpu=shares,
        ctx=tier.ctx,
        warnings=warnings,
    )


def recommend(
    gpu_sizes_mb: list[int],
    *,
    safety_margin_mb: int = DEFAULT_SAFETY_MARGIN_MB,
) -> Placement:
    """Plus grand palier réellement PLAÇABLE sur la topologie ; sinon infaisable (tier 0).

    Les paliers supérieurs écartés sont consignés dans `warnings` (valeur pédagogique :
    l'utilisateur voit *pourquoi* on n'a pas pris plus gros).
    """
    rejected: list[str] = []
    for tier in reversed(TIERS):  # du plus gourmand au plus léger
        placement = plan_for_tier(tier.gb, gpu_sizes_mb, safety_margin_mb=safety_margin_mb)
        if placement.feasible:
            return Placement(
                placement.tier_gb,
                True,
                placement.reason,
                gpu_indices=placement.gpu_indices,
                vram_mb=placement.vram_mb,
                vram_mb_per_gpu=placement.vram_mb_per_gpu,
                ctx=placement.ctx,
                warnings=placement.warnings + rejected,
            )
        rejected.append(f"palier {tier.gb} Go écarté : {placement.reason}")

    total = sum(int(s) for s in gpu_sizes_mb) if gpu_sizes_mb else 0
    hint = _split_hint([int(s) for s in gpu_sizes_mb], safety_margin_mb)
    return Placement(
        0,
        False,
        f"aucun palier LLM plaçable (VRAM totale {total} Mio) — transcription brute",
        warnings=rejected + ([hint] if hint else []),
    )


def _split_hint(sizes: list[int], safety_margin_mb: int) -> str | None:
    """Indice actionnable : un modèle tiendrait en split égal mais aucun profil livré ne le fait.

    Répond au cas « 2× 8 Go » : la VRAM agrégée suffit, mais les profils des petits
    paliers sont mono-GPU. On suggère alors un profil --tensor-split personnalisé,
    plutôt que de laisser l'utilisateur croire que sa machine est inapte.
    """
    if len(sizes) < 2:
        return None
    smallest = min(sizes)
    for tier in reversed(TIERS):
        if tier.profile_gpus != 1:
            continue  # déjà couvert par un profil multi-GPU
        share = ceil(tier.footprint_mb / len(sizes))
        if smallest >= share + safety_margin_mb:
            return (
                f"vos {len(sizes)} cartes hébergeraient {tier.label} (palier {tier.gb} Go) "
                f"en split (~{share} Mio/carte), mais aucun profil --tensor-split n'est "
                f"livré pour ce palier : un profil personnalisé serait nécessaire."
            )
    return None


# ── Vérification de calibration (déclaré vs réel mesuré) ────────────────────────


@dataclass(frozen=True)
class GpuCalibration:
    """État d'une carte du placement LLM : déclaré vs observé."""

    index: int
    declared_mb: int
    observed_mb: int
    total_mb: int
    free_mb: int
    level: str  # "ok" | "warn" | "critical"
    note: str


@dataclass(frozen=True)
class CalibrationReport:
    ok: bool
    per_gpu: list[GpuCalibration]
    warnings: list[str]
    suggested_vram_mb_per_gpu: list[int]
    suggested_vram_mb: int


def evaluate_calibration(
    *,
    declared_indices: list[int],
    declared_vram_mb: int,
    declared_per_gpu: list[int] | None,
    observed_per_gpu: dict[int, int],
    free_per_gpu: dict[int, int],
    total_per_gpu: dict[int, int],
    safety_margin_mb: int = DEFAULT_SAFETY_MARGIN_MB,
    drift_pct: int = DEFAULT_DRIFT_PCT,
) -> CalibrationReport:
    """Compare la calibration DÉCLARÉE (config) à la consommation RÉELLE mesurée.

    Aucune prédiction : `observed_per_gpu` est la VRAM réellement consommée par le
    process llama-server, carte par carte (poids + KV + compute compris). On signale :
      1. dérive déclaré/observé > `drift_pct` (calibration périmée),
      2. marge libre < `safety_margin_mb` (risque d'OOM imminent — critique),
      3. carte déclarée mais LLM absente (placement incohérent),
      4. LLM débordant sur une carte NON déclarée (--fit/--tensor-split divergent).

    Tous les signaux sont des avertissements : cette fonction ne décide ni ne tue rien.
    """
    indices = [int(i) for i in declared_indices]
    # Part déclarée par carte : la liste explicite si fournie et alignée, sinon split égal.
    if declared_per_gpu and len(declared_per_gpu) == len(indices):
        declared_map = {idx: int(mb) for idx, mb in zip(indices, declared_per_gpu)}
    elif indices:
        share = int(declared_vram_mb) // len(indices)
        declared_map = {idx: share for idx in indices}
    else:
        declared_map = {}

    per_gpu: list[GpuCalibration] = []
    warnings: list[str] = []
    suggested: list[int] = []
    all_ok = True

    for idx in indices:
        declared = declared_map.get(idx, 0)
        observed = int(observed_per_gpu.get(idx, 0))
        total = int(total_per_gpu.get(idx, 0))
        free = int(free_per_gpu.get(idx, 0))
        suggested.append(observed)

        level = "ok"
        note = "conforme"

        if observed <= 0:
            level = "warn"
            note = "carte déclarée pour la LLM mais aucune VRAM LLM observée (placement incohérent)"
            warnings.append(f"GPU {idx} : {note}.")
        else:
            drift = abs(observed - declared) / declared * 100 if declared > 0 else 100.0
            if drift > drift_pct:
                level = "warn"
                note = (
                    f"dérive {drift:.0f}% (déclaré {declared} Mio, observé {observed} Mio) "
                    f"— calibration probablement périmée"
                )
                warnings.append(f"GPU {idx} : {note}.")

        if 0 < free < safety_margin_mb:
            level = "critical"
            crit = f"marge critique {free} Mio libre (< {safety_margin_mb}) — risque d'OOM"
            note = crit if note == "conforme" else f"{note} ; {crit}"
            warnings.append(f"GPU {idx} : {crit}.")

        if level != "ok":
            all_ok = False
        per_gpu.append(
            GpuCalibration(idx, declared, observed, total, free, level, note)
        )

    # Débordement : LLM observée sur une carte hors placement déclaré.
    for idx, observed in observed_per_gpu.items():
        if int(observed) > 0 and int(idx) not in indices:
            all_ok = False
            warnings.append(
                f"GPU {idx} : la LLM y consomme {int(observed)} Mio alors qu'il n'est PAS "
                "dans llm_gpu_indices (--fit/--tensor-split divergent de la config)."
            )

    return CalibrationReport(
        ok=all_ok,
        per_gpu=per_gpu,
        warnings=warnings,
        suggested_vram_mb_per_gpu=suggested,
        suggested_vram_mb=sum(suggested),
    )

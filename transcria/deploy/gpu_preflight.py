"""Preflight GPU pour le quickstart Docker (mode GPU / image `:bundled`).

Vérifie AVANT le build/pull et le démarrage qu'au moins un GPU satisfait les exigences
de l'image all-in-one, pour échouer **tôt avec un message clair** plutôt que de laisser
un crash CUDA cryptique survenir au premier job.

Limites (cf. docs/DOCKER.md § Prérequis GPU / VRAM) :
  * **compute capability ≥ 7.5** : `llama-server` est compilé en SASS Turing→Hopper
    (75;80;86;89;90) + PTX `sm_90`. Le PTX ne fait du JIT que vers le **HAUT** (Blackwell
    ≥ sm_100) — une carte < 7.5 (Pascal 10xx = 6.x, Volta = 7.0) ne peut donc PAS être
    couverte et n'est pas supportée.
  * **VRAM** : seuils PAR MODE depuis 2026-08-07. Slim (défaut) ≥ ~8 Go — le palier LLM 8
    (Qwen3.5-4B, pic ~6,4 Go) se télécharge au 1ᵉʳ run ; les phases sont séquencées par
    l'autonomie VRAM (non additives), la marge suit le plus gros pic. Bundled ≥ ~12 Go —
    l'image bake la LLM du palier 12 (9B, ~10,6 Go). Sous le seuil = refus ; juste
    au-dessus = avertissement.

Module **stdlib pur** (pas d'import lourd) : le quickstart l'exécute côté hôte, sans le venv
du projet. `classify_gpu` / `parse_nvidia_smi_csv` sont des fonctions pures (testables).
"""
from __future__ import annotations

import subprocess
import sys

MIN_COMPUTE = 7.5
# Plancher SLIM (défaut) abaissé au palier LLM 8 Go (Qwen3.5-4B, 2026-08-07) : l'image
# slim télécharge ses modèles au 1ᵉʳ run dans les volumes hôte — sur une carte 8 Go, le
# palier 8 est servi (pic maximal ~6,4 Go, phases séquencées par l'autonomie VRAM).
# Depuis 0.4.2 la BUNDLED bake AUSSI la LLM du palier 8 (Qwen3.5-4B) et l'entrypoint
# rétrograde automatiquement < 12 Go : mêmes seuils. Les constantes restent distinctes
# pour pouvoir diverger à nouveau si une future bundled ne bake que de gros modèles.
MIN_VRAM_MB = 7_500
RECOMMENDED_VRAM_MB = 8_192
BUNDLED_MIN_VRAM_MB = 7_500
BUNDLED_RECOMMENDED_VRAM_MB = 8_192

# Statuts de verdict, du meilleur au pire.
OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def classify_gpu(
    compute_cap: float,
    vram_mb: int,
    *,
    min_vram_mb: int = MIN_VRAM_MB,
    recommended_vram_mb: int = RECOMMENDED_VRAM_MB,
) -> tuple[str, str]:
    """Classe UN GPU. Retourne (statut, message actionnable).

    Les seuils VRAM sont paramétrables : slim (défaut, palier 8 Go téléchargé au
    runtime) vs bundled (palier 12 baké — passer les seuils BUNDLED_*)."""
    if compute_cap < MIN_COMPUTE:
        return (
            FAIL,
            f"compute capability {compute_cap:g} < {MIN_COMPUTE:g} — carte non supportée "
            "(Pascal/Volta). Le binaire LLM ne peut pas tourner dessus. Voir la table de "
            "compatibilité dans docs/DOCKER.md.",
        )
    if vram_mb < min_vram_mb:
        return (
            FAIL,
            f"VRAM {vram_mb} Mo < {min_vram_mb} Mo — insuffisant pour la LLM d'arbitrage "
            f"de ce mode + STT/diarisation. Une carte ≥ {recommended_vram_mb // 1024} Go est requise.",
        )
    if vram_mb < recommended_vram_mb:
        return (
            WARN,
            f"VRAM {vram_mb} Mo proche de la limite (~{recommended_vram_mb // 1024} Go "
            "recommandés) — devrait fonctionner mais peut être juste selon l'audio. "
            "Surveiller l'admission GPU.",
        )
    return (OK, f"compute {compute_cap:g}, VRAM {vram_mb} Mo — compatible.")


def parse_nvidia_smi_csv(text: str) -> list[tuple[float, int]]:
    """Parse la sortie `--query-gpu=compute_cap,memory.total --format=csv,noheader,nounits`.

    Une ligne par GPU : « 7.5, 12288 ». Tolère espaces et lignes vides ; ignore les lignes
    non parsables (robustesse face à un pilote bavard).
    """
    gpus: list[tuple[float, int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            compute = float(parts[0])
            vram = int(float(parts[1]))
        except ValueError:
            continue
        gpus.append((compute, vram))
    return gpus


def evaluate(gpus: list[tuple[float, int]], *, bundled: bool = False) -> tuple[str, str]:
    """Verdict global : le MEILLEUR GPU détermine l'issue (il suffit qu'un GPU convienne).

    Retourne (statut, message). Aucun GPU détecté ⇒ échec. ``bundled`` applique
    les seuils de l'image à modèles embarqués (palier 12 baké)."""
    if not gpus:
        return (FAIL, "aucun GPU détecté par nvidia-smi — driver NVIDIA absent ou GPU masqué.")
    min_v = BUNDLED_MIN_VRAM_MB if bundled else MIN_VRAM_MB
    rec_v = BUNDLED_RECOMMENDED_VRAM_MB if bundled else RECOMMENDED_VRAM_MB
    best_status = FAIL
    best_msg = ""
    for idx, (compute, vram) in enumerate(gpus):
        status, msg = classify_gpu(compute, vram, min_vram_mb=min_v, recommended_vram_mb=rec_v)
        labelled = f"GPU {idx}: {msg}"
        if _RANK[status] < _RANK[best_status] or best_msg == "":
            best_status, best_msg = status, labelled
        if status == OK:
            break
    return (best_status, best_msg)


def _query_nvidia_smi() -> str:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=compute_cap,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def main(argv: list[str] | None = None) -> int:
    """Exécute le preflight réel. Code retour 0 = ok/avertissement, 1 = échec/bloquant."""
    try:
        raw = _query_nvidia_smi()
    except Exception as exc:  # noqa: BLE001 — outil best-effort, message clair
        print(f"[ERROR] preflight GPU : nvidia-smi a échoué ({exc}).", file=sys.stderr)
        return 1

    bundled = "--bundled" in (argv or [])
    status, message = evaluate(parse_nvidia_smi_csv(raw), bundled=bundled)
    if status == FAIL:
        print(f"[ERROR] GPU incompatible — {message}", file=sys.stderr)
        return 1
    if status == WARN:
        print(f"[WARN] {message}", file=sys.stderr)
        return 0
    print(f"[OK] preflight GPU — {message}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))

"""Preflight GPU pour le quickstart Docker (mode GPU / image `:bundled`).

Vérifie AVANT le build/pull et le démarrage qu'au moins un GPU satisfait les exigences
de l'image all-in-one, pour échouer **tôt avec un message clair** plutôt que de laisser
un crash CUDA cryptique survenir au premier job.

Limites (cf. docs/DOCKER.md § Prérequis GPU / VRAM) :
  * **compute capability ≥ 7.5** : `llama-server` est compilé en SASS Turing→Hopper
    (75;80;86;89;90) + PTX `sm_90`. Le PTX ne fait du JIT que vers le **HAUT** (Blackwell
    ≥ sm_100) — une carte < 7.5 (Pascal 10xx = 6.x, Volta = 7.0) ne peut donc PAS être
    couverte et n'est pas supportée.
  * **VRAM ≥ ~12 Go** : la LLM d'arbitrage (Qwen3.5-9B Q5_K_M) monte à ~10,6 Go ; whisper
    et Sortformer sont séquencés par l'autonomie VRAM (non additifs), mais il faut la marge
    du plus gros pic. < 11,5 Go = refus ; 11,5–12 Go = avertissement (limite).

Module **stdlib pur** (pas d'import lourd) : le quickstart l'exécute côté hôte, sans le venv
du projet. `classify_gpu` / `parse_nvidia_smi_csv` sont des fonctions pures (testables).
"""
from __future__ import annotations

import subprocess
import sys

MIN_COMPUTE = 7.5
MIN_VRAM_MB = 11_500
RECOMMENDED_VRAM_MB = 12_288

# Statuts de verdict, du meilleur au pire.
OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def classify_gpu(compute_cap: float, vram_mb: int) -> tuple[str, str]:
    """Classe UN GPU. Retourne (statut, message actionnable)."""
    if compute_cap < MIN_COMPUTE:
        return (
            FAIL,
            f"compute capability {compute_cap:g} < {MIN_COMPUTE:g} — carte non supportée "
            "(Pascal/Volta). Le binaire LLM ne peut pas tourner dessus. Voir la table de "
            "compatibilité dans docs/DOCKER.md.",
        )
    if vram_mb < MIN_VRAM_MB:
        return (
            FAIL,
            f"VRAM {vram_mb} Mo < {MIN_VRAM_MB} Mo — insuffisant pour la LLM d'arbitrage "
            "(~10,6 Go) + STT/diarisation. Une carte ≥ 12 Go est requise.",
        )
    if vram_mb < RECOMMENDED_VRAM_MB:
        return (
            WARN,
            f"VRAM {vram_mb} Mo proche de la limite (~12 Go recommandés) — devrait fonctionner "
            "mais peut être juste selon l'audio. Surveiller l'admission GPU.",
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


def evaluate(gpus: list[tuple[float, int]]) -> tuple[str, str]:
    """Verdict global : le MEILLEUR GPU détermine l'issue (il suffit qu'un GPU convienne).

    Retourne (statut, message). Aucun GPU détecté ⇒ échec.
    """
    if not gpus:
        return (FAIL, "aucun GPU détecté par nvidia-smi — driver NVIDIA absent ou GPU masqué.")
    best_status = FAIL
    best_msg = ""
    for idx, (compute, vram) in enumerate(gpus):
        status, msg = classify_gpu(compute, vram)
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

    status, message = evaluate(parse_nvidia_smi_csv(raw))
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

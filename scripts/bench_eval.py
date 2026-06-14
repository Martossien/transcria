#!/usr/bin/env python3
"""
TranscrIA — évaluation LLM des SRTs produits par bench_audio.py.

Lit les SRTs issus d'un répertoire de bench, les envoie à la LLM d'arbitrage
avec un prompt de notation multicritères, et génère un rapport Markdown.

Utilisation :
    python scripts/bench_eval.py --bench-dir bench_results/test2_20260521_143000

Avec LLM sur port alternatif :
    python scripts/bench_eval.py \\
        --bench-dir bench_results/test2_20260521_143000 \\
        --arbitrage-port 8080

Comparer les SRTs corrigés (si LLM activée lors du bench) :
    python scripts/bench_eval.py \\
        --bench-dir bench_results/test2_20260521_143000 \\
        --srt-type corrected

Tronquer à 2000 mots par SRT (pour gros fichiers) :
    python scripts/bench_eval.py \\
        --bench-dir bench_results/test2_20260521_143000 \\
        --max-words 2000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).parent.parent
PROMPT_FILE = REPO_ROOT / "prompts" / "bench_eval_prompt.txt"
DEFAULT_PORT = 8080
DEFAULT_MODEL = "arbitrage"
DEFAULT_MAX_WORDS = 3000

logger = logging.getLogger("bench_eval")


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Évaluation LLM des SRTs produits par bench_audio.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--bench-dir", type=Path, required=True,
        help="Répertoire de sortie d'un run bench_audio.py",
    )
    parser.add_argument(
        "--srt-type", choices=["raw", "corrected"], default="raw",
        help="Type de SRT à évaluer : 'raw' (transcription.srt) ou "
             "'corrected' (transcription_corrigee.srt, défaut: raw)",
    )
    parser.add_argument(
        "--combos", type=str, default=None,
        help="Sous-ensemble de combos à évaluer, ex: '001,005,013' "
             "(défaut: tous les combos avec JSON dans --bench-dir)",
    )
    parser.add_argument(
        "--max-words", type=int, default=DEFAULT_MAX_WORDS,
        help=f"Nombre max de mots par SRT avant troncature (défaut: {DEFAULT_MAX_WORDS})",
    )
    parser.add_argument(
        "--arbitrage-port", type=int, default=DEFAULT_PORT,
        help=f"Port de la LLM d'arbitrage (défaut: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--model-id", type=str, default=DEFAULT_MODEL,
        help=f"Alias modèle LLM (défaut: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Fichier Markdown de sortie (défaut: <bench-dir>/eval_report.md)",
    )
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="Timeout en secondes pour l'appel LLM (défaut: 600)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Afficher le prompt complet sans appeler la LLM",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Lecture des données de bench
# ─────────────────────────────────────────────────────────────────────────────
def load_bench_results(bench_dir: Path) -> list[dict]:
    """Charge tous les JSON de résultats du répertoire de bench."""
    results = []
    # Support both numeric IDs (001.json) and E-prefixed IDs (E01.json)
    patterns = ["[0-9][0-9][0-9].json", "E[0-9][0-9].json"]
    seen: set[Path] = set()
    for pattern in patterns:
        for json_path in sorted(bench_dir.glob(pattern)):
            if json_path in seen:
                continue
            seen.add(json_path)
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                results.append(data)
            except Exception as exc:
                logger.warning("JSON illisible : %s (%s)", json_path.name, exc)
    results.sort(key=lambda r: r.get("combo_id") or "")
    return results


def find_srt(result: dict, srt_type: str) -> Path | None:
    """Trouve le fichier SRT correspondant au type demandé."""
    srt_data = result.get("srt") or {}
    if srt_type == "corrected":
        p = srt_data.get("corrected_path")
        if p:
            path = Path(p)
            if path.exists():
                return path
        logger.warning("[%s] SRT corrigé absent — fallback sur brut", result.get("combo_id", "?"))

    p = srt_data.get("raw_path")
    if p:
        path = Path(p)
        if path.exists():
            return path

    # Fallback : chercher dans job_dir
    job_dir = result.get("job_dir")
    if job_dir:
        for name in ("transcription_corrigee.srt", "transcription.srt"):
            candidate = Path(job_dir) / "metadata" / name
            if srt_type == "raw" and "corrigee" in name:
                continue
            if candidate.exists():
                return candidate

    return None


def truncate_srt(content: str, max_words: int) -> tuple[str, bool]:
    """Tronque un SRT à max_words mots, retourne (contenu, tronqué)."""
    words = content.split()
    if len(words) <= max_words:
        return content, False
    truncated = " ".join(words[:max_words])
    return truncated + "\n\n[... SRT tronqué à " + str(max_words) + " mots ...]", True


def combo_flags(result: dict) -> str:
    """Formate les flags d'un combo pour l'en-tête SRT."""
    parts = [
        f"STT={result.get('stt_backend', '?')}",
        f"scene={int(bool(result.get('audio_scene', False)))}",
        f"sep={int(bool(result.get('source_separation', False)))}",
        f"norm={int(bool(result.get('audio_normalization', False)))}",
        f"filter={int(bool(result.get('scene_filter', False)))}",
    ]
    return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Construction du prompt
# ─────────────────────────────────────────────────────────────────────────────
def build_prompt(
    results: list[dict],
    srt_type: str,
    max_words: int,
) -> tuple[str, list[dict]]:
    """
    Construit le contenu utilisateur à envoyer à la LLM.

    Retourne (contenu_user, liste_des_entries_avec_srt_trouvé).
    """
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")

    entries = []
    srt_blocks = []
    skipped = []

    for result in results:
        combo_id = result.get("combo_id", "?")
        if result.get("status") != "ok":
            skipped.append(f"{combo_id} (status={result.get('status')})")
            continue

        srt_path = find_srt(result, srt_type)
        if srt_path is None:
            skipped.append(f"{combo_id} (SRT introuvable)")
            continue

        try:
            content = srt_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            skipped.append(f"{combo_id} (lecture erreur: {exc})")
            continue

        content, was_truncated = truncate_srt(content, max_words)
        flags = combo_flags(result)
        header = f"## SRT-{combo_id} [{flags}]"
        if was_truncated:
            header += f" [TRONQUÉ à {max_words} mots]"

        srt_blocks.append(f"{header}\n\n{content}")
        entries.append({"combo_id": combo_id, "flags": flags, "srt_path": str(srt_path)})

    if skipped:
        logger.warning("Combos ignorés : %s", ", ".join(skipped))

    user_content = "\n\n---\n\n".join(srt_blocks)

    return system_prompt, user_content, entries


# ─────────────────────────────────────────────────────────────────────────────
# Appel LLM
# ─────────────────────────────────────────────────────────────────────────────
def call_llm(
    system_prompt: str,
    user_content: str,
    port: int,
    model_id: str,
    timeout: int,
) -> str:
    """Appelle la LLM d'arbitrage et retourne le texte de réponse."""
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "top_p": 0.95,
        "max_tokens": 8192,
        "stream": False,
    }

    logger.info("Appel LLM : %s (modèle=%s, timeout=%ds)", url, model_id, timeout)
    logger.info(
        "Prompt : system=%d mots, user=%d mots",
        len(system_prompt.split()),
        len(user_content.split()),
    )

    t0 = time.monotonic()
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        logger.error(
            "Impossible de joindre la LLM sur le port %d — "
            "vérifier que la LLM d'arbitrage est démarrée", port,
        )
        raise
    except requests.exceptions.Timeout:
        logger.error("Timeout LLM après %ds", timeout)
        raise

    elapsed = time.monotonic() - t0
    data = response.json()
    text = data["choices"][0]["message"]["content"]
    tokens_in = data.get("usage", {}).get("prompt_tokens", "?")
    tokens_out = data.get("usage", {}).get("completion_tokens", "?")
    logger.info(
        "Réponse reçue en %.1fs — tokens entrée=%s sortie=%s",
        elapsed, tokens_in, tokens_out,
    )
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Rapport Markdown
# ─────────────────────────────────────────────────────────────────────────────
def write_report(
    llm_response: str,
    entries: list[dict],
    args: argparse.Namespace,
    bench_dir: Path,
    elapsed_s: float,
) -> Path:
    output_path = args.output or (bench_dir / "eval_report.md")

    header = [
        f"# Rapport d'évaluation LLM — {bench_dir.name}",
        "",
        f"- Bench dir    : `{bench_dir}`",
        f"- Type SRT     : {args.srt_type}",
        f"- Combos évalués : {len(entries)} ({', '.join(e['combo_id'] for e in entries)})",
        f"- Max mots/SRT : {args.max_words}",
        f"- Modèle LLM   : {args.model_id} (port {args.arbitrage_port})",
        f"- Généré       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Durée éval   : {elapsed_s:.0f}s",
        "",
        "---",
        "",
    ]

    content = "\n".join(header) + llm_response + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()

    bench_dir = args.bench_dir
    if not bench_dir.exists():
        logger.error("Répertoire de bench introuvable : %s", bench_dir)
        return 1

    if not PROMPT_FILE.exists():
        logger.error("Prompt introuvable : %s", PROMPT_FILE)
        return 1

    # ── Chargement des résultats ─────────────────────────────────────────────
    all_results = load_bench_results(bench_dir)
    if not all_results:
        logger.error("Aucun JSON de résultat trouvé dans %s", bench_dir)
        return 1
    logger.info("%d JSON de résultats chargés", len(all_results))

    # Filtre optionnel par IDs
    if args.combos:
        requested = {cid.strip().zfill(3) for cid in args.combos.split(",")}
        all_results = [r for r in all_results if r.get("combo_id") in requested]
        logger.info("Après filtre --combos : %d résultats", len(all_results))

    # ── Construction du prompt ───────────────────────────────────────────────
    try:
        system_prompt, user_content, entries = build_prompt(
            all_results, args.srt_type, args.max_words
        )
    except Exception as exc:
        logger.error("Erreur lors de la construction du prompt : %s", exc)
        return 1

    if not entries:
        logger.error("Aucun SRT valide à évaluer")
        return 1

    logger.info("%d SRT(s) prêts pour évaluation", len(entries))
    for e in entries:
        logger.info("  %s  [%s]", e["combo_id"], e["flags"])

    # ── Dry-run ──────────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n" + "=" * 72)
        print("SYSTEM PROMPT :")
        print("=" * 72)
        print(system_prompt[:2000], "...[tronqué]" if len(system_prompt) > 2000 else "")
        print("\n" + "=" * 72)
        print(f"USER CONTENT ({len(user_content.split())} mots) :")
        print("=" * 72)
        print(user_content[:3000], "...[tronqué]" if len(user_content) > 3000 else "")
        return 0

    # ── Appel LLM ────────────────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        llm_response = call_llm(
            system_prompt, user_content,
            args.arbitrage_port, args.model_id, args.timeout,
        )
    except Exception as exc:
        logger.error("Échec appel LLM : %s", exc)
        return 1

    elapsed = time.monotonic() - t0

    # ── Rapport ──────────────────────────────────────────────────────────────
    report_path = write_report(llm_response, entries, args, bench_dir, elapsed)
    logger.info("Rapport écrit : %s", report_path)

    print("\n" + "=" * 72)
    print(llm_response)
    print("=" * 72)
    print(f"\n  Rapport complet : {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prépare une campagne E2E A/B/C pour arbitrage hybride LLM.

Chaque audio produit trois jobs conservés :
- A : Cohere, options prudentes
- B : Whisper large-v3, options prudentes
- C : Whisper large-v3 + hotwords lexique

Les runs gardent la phase résumé/diarisation pour disposer des locuteurs, mais
désactivent la LLM de résumé/correction. L'arbitrage LLM se lance ensuite avec
scripts/arbitrate_hybrid_llm.py à partir des job_ids produits.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s [prepare_hybrid_llm_bench] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("prepare_hybrid_llm_bench")

DEFAULT_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}


@dataclass(frozen=True)
class AudioCase:
    path: Path
    slug: str


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "audio"


def discover_audio_files(roots: list[Path], limit: int | None) -> list[AudioCase]:
    cases: list[AudioCase] = []
    seen: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix.lower() in DEFAULT_AUDIO_EXTS:
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(path for path in root.rglob("*") if path.suffix.lower() in DEFAULT_AUDIO_EXTS)
        else:
            logger.warning("Audio root ignoré: %s", root)
            continue
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            cases.append(AudioCase(path=resolved, slug=_slug(path.stem)))
            if limit and len(cases) >= limit:
                return cases
    return cases


def _base_e2e_cmd(audio: AudioCase, variant: str, args: argparse.Namespace, gpu: str | None) -> list[str]:
    combo_id = f"{audio.slug}-{variant}"
    output_json = args.output_dir / f"{combo_id}.json"
    backend = "cohere" if variant == "A-cohere" else "whisper"
    cmd = [
        "venv/bin/python",
        "tests/test_e2e_workflow.py",
        "--audio",
        str(audio.path),
        "--job-title",
        f"Hybrid LLM bench {audio.slug} {variant}",
        "--combo-id",
        combo_id,
        "--output-json",
        str(output_json),
        "--stt-backend",
        backend,
        "--whisper-model-size",
        args.whisper_model_size,
        "--mode",
        args.mode,
        "--skip-llm",
        "--keep",
        "--keep-on-error",
    ]
    if gpu:
        cmd.extend(["--gpu", gpu])
    if args.lexicon_json:
        cmd.extend(["--lexicon-json", str(args.lexicon_json)])
    for override in args.config_override:
        cmd.extend(["--config-override", override])
    if variant == "C-whisper-hotwords":
        cmd.append("--enable-whisper-lexicon-hotwords")
    if variant == "A-cohere" and args.enable_cohere_lexicon_biasing:
        cmd.append("--enable-cohere-lexicon-biasing")
    return cmd


def build_manifest(args: argparse.Namespace) -> dict:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    audios = discover_audio_files(args.audio_root, args.limit)
    if not audios:
        raise SystemExit("Aucun fichier audio trouvé")
    gpus = [gpu.strip() for gpu in (args.gpus or "").split(",") if gpu.strip()]
    variants = ["A-cohere", "B-whisper", "C-whisper-hotwords"]
    runs: list[dict] = []
    groups: list[dict] = []
    run_index = 0
    for audio in audios:
        group_runs: dict[str, str] = {}
        for variant in variants:
            gpu = gpus[run_index % len(gpus)] if gpus else None
            cmd = _base_e2e_cmd(audio, variant, args, gpu)
            combo_id = f"{audio.slug}-{variant}"
            runs.append({
                "combo_id": combo_id,
                "audio_path": str(audio.path),
                "variant": variant,
                "gpu": gpu,
                "output_json": str(args.output_dir / f"{combo_id}.json"),
                "command": cmd,
            })
            group_runs[variant] = str(args.output_dir / f"{combo_id}.json")
            run_index += 1
        groups.append({
            "audio_slug": audio.slug,
            "audio_path": str(audio.path),
            "runs": group_runs,
            "arbitration_output_json": str(args.output_dir / "hybrid" / f"{audio.slug}_arbitration.json"),
            "arbitration_output_srt": str(args.output_dir / "hybrid" / f"{audio.slug}_hybrid.srt"),
        })
    return {
        "purpose": "A/B/C STT runs for LLM hybrid arbitration",
        "mode": args.mode,
        "with_speakers": True,
        "llm_in_e2e": False,
        "audio_count": len(audios),
        "run_count": len(runs),
        "max_parallel": args.max_parallel or max(1, min(len(gpus) or 1, len(runs))),
        "runs": runs,
        "groups": groups,
    }


def _quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def write_shell(manifest: dict, output: Path, parallel: bool) -> None:
    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"max_parallel={int(manifest['max_parallel'])}",
        "failures=0",
        "",
    ]
    if parallel:
        lines.extend([
            "running=0",
            "run_one() {",
            "  local label=\"$1\"",
            "  shift",
            "  echo \"[hybrid-llm-bench] ${label}\"",
            "  \"$@\"",
            "}",
            "",
        ])
        for run in manifest["runs"]:
            lines.append(f"run_one {shlex.quote(run['combo_id'])} {_quote_cmd(run['command'])} &")
            lines.extend([
                "running=$((running + 1))",
                "if [ \"$running\" -ge \"$max_parallel\" ]; then",
                "  if ! wait -n; then failures=$((failures + 1)); fi",
                "  running=$((running - 1))",
                "fi",
                "",
            ])
        lines.extend([
            "while [ \"$running\" -gt 0 ]; do",
            "  if ! wait -n; then failures=$((failures + 1)); fi",
            "  running=$((running - 1))",
            "done",
        ])
    else:
        for run in manifest["runs"]:
            lines.extend([
                f"echo '[hybrid-llm-bench] {run['combo_id']}'",
                f"if ! {_quote_cmd(run['command'])}; then failures=$((failures + 1)); fi",
                "",
            ])
    lines.extend([
        "if [ \"$failures\" -ne 0 ]; then",
        "  echo \"[hybrid-llm-bench] ${failures} run(s) en échec\" >&2",
        "  exit 1",
        "fi",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(0o755)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prépare les runs A/B/C pour arbitrage hybride LLM.")
    parser.add_argument("--audio-root", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/transcria_hybrid_llm_bench"))
    parser.add_argument("--manifest", type=Path, default=Path("/tmp/transcria_hybrid_llm_bench/manifest.json"))
    parser.add_argument("--script", type=Path, default=Path("/tmp/transcria_hybrid_llm_bench/run_hybrid_llm_bench.sh"))
    parser.add_argument("--mode", choices=["fast", "quality"], default="fast")
    parser.add_argument("--whisper-model-size", default="large-v3")
    parser.add_argument("--gpus", default="")
    parser.add_argument("--max-parallel", type=int)
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--lexicon-json", type=Path)
    parser.add_argument("--enable-cohere-lexicon-biasing", action="store_true")
    parser.add_argument("--config-override", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = build_manifest(args)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_shell(manifest, args.script, args.parallel)
    logger.info("Manifest écrit: %s", args.manifest)
    logger.info("Script écrit: %s (%d runs)", args.script, manifest["run_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

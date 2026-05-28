#!/usr/bin/env python3
"""Prépare des lots E2E Whisper baseline vs hotwords.

Le script ne lance pas les transcriptions. Il génère un manifeste JSON et un
script shell auditable pour exécuter les paires de runs sur les audios de test,
archives et réunions réelles.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s [prepare_hotwords_bench] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("prepare_hotwords_bench")

DEFAULT_AUDIO_PATTERNS = ("*.mp3", "*.wav", "*.m4a", "*.flac", "*.ogg")


@dataclass(frozen=True)
class AudioCase:
    path: Path
    source: str
    slug: str


def discover_audio_files(roots: list[Path], patterns: tuple[str, ...] = DEFAULT_AUDIO_PATTERNS) -> list[AudioCase]:
    cases: list[AudioCase] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            logger.warning("Répertoire audio absent, ignoré: %s", root)
            continue
        files: list[Path]
        if root.is_file():
            files = [root]
            source = root.parent.name
        else:
            files = []
            for pattern in patterns:
                files.extend(root.glob(pattern))
            source = root.name
        for path in sorted(files):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            cases.append(AudioCase(path=resolved, source=source, slug=_slug(path.stem)))
    return cases


def _slug(value: str) -> str:
    allowed = []
    for char in value.lower():
        if char.isalnum():
            allowed.append(char)
        elif allowed and allowed[-1] != "-":
            allowed.append("-")
    return "".join(allowed).strip("-") or "audio"


def _command_for(
    *,
    audio: AudioCase,
    variant: str,
    output_dir: Path,
    gpu: str,
    mode: str,
    keep: bool,
    skip_llm: bool,
    skip_summary: bool,
    skip_diarization: bool,
    lexicon_json: Path | None,
) -> list[str]:
    combo_id = f"{audio.source}-{audio.slug}-whisper-{variant}"
    output_json = output_dir / f"{combo_id}.json"
    cmd = [
        "venv/bin/python",
        "tests/test_e2e_workflow.py",
        "--audio",
        str(audio.path),
        "--job-title",
        f"Bench hotwords {audio.source} {audio.slug} {variant}",
        "--stt-backend",
        "whisper",
        "--whisper-model-size",
        "large-v3",
        "--mode",
        mode,
        "--gpu",
        gpu,
        "--combo-id",
        combo_id,
        "--output-json",
        str(output_json),
    ]
    if keep:
        cmd.append("--keep")
    if skip_llm:
        cmd.append("--skip-llm")
    if skip_summary:
        cmd.append("--skip-summary")
    if skip_diarization:
        cmd.append("--skip-diarization")
    if lexicon_json is not None:
        cmd.extend(["--lexicon-json", str(lexicon_json)])
    if variant == "hotwords":
        cmd.append("--enable-whisper-lexicon-hotwords")
    return cmd


def build_manifest(args: argparse.Namespace) -> dict:
    roots = args.audio_root or [
        Path("tests"),
        Path("archives/audio_tests"),
        Path("/home/admin_ia/Téléchargements/reunion_son"),
    ]
    cases = discover_audio_files(roots)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("Aucun fichier audio trouvé")

    gpus = [str(gpu).strip() for gpu in args.gpus.split(",") if str(gpu).strip()]
    if not gpus:
        gpus = ["0"]
    max_parallel = int(args.max_parallel or len(gpus))
    max_parallel = max(1, min(max_parallel, len(gpus)))

    runs = []
    for index, audio in enumerate(cases):
        for variant_index, variant in enumerate(("baseline", "hotwords")):
            gpu = gpus[(index * 2 + variant_index) % len(gpus)]
            cmd = _command_for(
                audio=audio,
                variant=variant,
                output_dir=args.output_dir,
                gpu=gpu,
                mode=args.mode,
                keep=args.keep,
                skip_llm=args.skip_llm,
                skip_summary=args.skip_summary,
                skip_diarization=args.skip_diarization,
                lexicon_json=args.lexicon_json,
            )
            runs.append({
                "audio": str(audio.path),
                "source": audio.source,
                "slug": audio.slug,
                "variant": variant,
                "gpu": gpu,
                "combo_id": f"{audio.source}-{audio.slug}-whisper-{variant}",
                "output_json": str(args.output_dir / f"{audio.source}-{audio.slug}-whisper-{variant}.json"),
                "command": cmd,
            })

    return {
        "purpose": "Whisper baseline vs lexicon hotwords",
        "mode": args.mode,
        "skip_llm": args.skip_llm,
        "skip_summary": args.skip_summary,
        "skip_diarization": args.skip_diarization,
        "keep": args.keep,
        "lexicon_json": str(args.lexicon_json) if args.lexicon_json else None,
        "gpus": gpus,
        "max_parallel": max_parallel,
        "audio_count": len(cases),
        "run_count": len(runs),
        "runs": runs,
    }


def write_shell(manifest: dict, output: Path, parallel: bool) -> None:
    max_parallel = int(manifest.get("max_parallel") or 1)
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail" if parallel else "set -euo pipefail",
        "",
        f"mkdir -p {shlex.quote(str(Path(manifest['runs'][0]['output_json']).parent))}",
        "",
    ]
    if parallel:
        lines.extend([
            "running=0",
            "failures=0",
            f"max_parallel={max_parallel}",
            "",
        ])
    for run in manifest["runs"]:
        command = " ".join(shlex.quote(part) for part in run["command"])
        lines.append(f"echo '[hotwords-bench] {run['combo_id']}'")
        if parallel:
            lines.append(f"{command} &")
            lines.append("running=$((running + 1))")
            lines.append('if [ "$running" -ge "$max_parallel" ]; then')
            lines.append("  if ! wait -n; then")
            lines.append("    failures=$((failures + 1))")
            lines.append("  fi")
            lines.append("  running=$((running - 1))")
            lines.append("fi")
        else:
            lines.append(command)
        lines.append("")
    if parallel:
        lines.extend([
            'while [ "$running" -gt 0 ]; do',
            "  if ! wait -n; then",
            "    failures=$((failures + 1))",
            "  fi",
            "  running=$((running - 1))",
            "done",
            'if [ "$failures" -ne 0 ]; then',
            '  echo "[hotwords-bench] ${failures} run(s) en échec" >&2',
            "  exit 1",
            "fi",
        ])
    output.write_text("\n".join(lines), encoding="utf-8")
    output.chmod(0o755)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prépare les runs E2E Whisper baseline vs hotwords.")
    parser.add_argument("--audio-root", type=Path, action="append", help="Répertoire ou fichier audio. Répétable.")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/transcria_hotwords_bench"))
    parser.add_argument("--manifest", type=Path, default=Path("/tmp/transcria_hotwords_bench/manifest.json"))
    parser.add_argument("--script", type=Path, default=Path("/tmp/transcria_hotwords_bench/run_hotwords_bench.sh"))
    parser.add_argument("--gpus", default="3,5,6,7")
    parser.add_argument("--mode", default="fast", choices=["fast", "quality"])
    parser.add_argument("--lexicon-json", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--keep", action="store_true", default=True)
    parser.add_argument("--no-keep", action="store_false", dest="keep")
    parser.add_argument("--skip-llm", action="store_true", help="Plus rapide, mais nécessite --lexicon-json pour tester réellement les hotwords.")
    parser.add_argument("--skip-summary", action="store_true", help="Ignore aussi la transcription rapide Cohere du résumé. Recommandé avec --skip-llm --lexicon-json.")
    parser.add_argument("--skip-diarization", action="store_true", default=True)
    parser.add_argument("--with-diarization", action="store_false", dest="skip_diarization")
    parser.add_argument("--parallel", action="store_true", help="Écrit un script shell qui lance les runs en parallèle.")
    parser.add_argument("--max-parallel", type=int, help="Nombre maximum de runs simultanés dans le script --parallel. Défaut: nombre de GPUs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.script.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(args)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_shell(manifest, args.script, args.parallel)
    logger.info("Manifeste écrit: %s (%d runs)", args.manifest, manifest["run_count"])
    logger.info("Script écrit: %s", args.script)
    if manifest["skip_llm"] and not manifest["lexicon_json"]:
        logger.warning("--skip-llm sans --lexicon-json: les hotwords n'auront probablement aucun lexique de session à injecter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

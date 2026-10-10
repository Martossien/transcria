#!/usr/bin/env python3
"""Banc « attribution des locuteurs à l'étape Résumé » (docs/ATTRIBUTION_LOCUTEURS_RESUME.md).

Joue la SEULE phase Résumé (STT rapide → diarisation → LLM) sur un audio réel, dans un mode
donné, et écrit ce qu'il faut pour JUGER l'attribution des « Participants probables » :

- ``speaker_roles_llm`` (ce que la LLM a attribué à chaque SPEAKER_XX) ;
- le transcript rapide tel que la LLM l'a lu, et sa COUVERTURE étiquetée (part des mots
  portant un locuteur) ;
- les stats de diarisation (temps de parole par locuteur) et les lignes « certaines » du
  contexte de diarisation ;
- les temps (diarisation, STT, LLM, total).

Modes : ``baseline`` (ordre historique : STT par blocs, diarisation ensuite) et ``by_turns``
(``workflow.summary_by_turns`` : diarisation d'abord, STT par tours, transcript étiqueté).

Usage :
    venv/bin/python scripts/bench/attribution_bench.py --audio /chemin/reunion.wav \\
        --label M1 --mode by_turns [--override models.diarization_backend=auto] \\
        [--out bench_results/attribution]

Le job est CONSERVÉ (titre « BENCH attribution … ») pour inspection ; aucun fichier du corpus
ne sort de la machine (bench_results/ est ignoré par git).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
os.environ.setdefault("TRANSCRIA_CONFIG", str(_REPO / "config.yaml"))


def _parse_value(raw: str):
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith(("[", "{")):
        return json.loads(raw)
    return raw


def _set_nested(cfg: dict, dotted: str, value) -> None:
    node = cfg
    keys = dotted.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", type=Path, required=True)
    ap.add_argument("--label", required=True, help="étiquette de la réunion (ex. M1)")
    ap.add_argument("--mode", choices=("baseline", "by_turns"), required=True)
    ap.add_argument("--override", action="append", default=[], help="CLE.POINTEE=VALEUR (répétable)")
    ap.add_argument("--out", type=Path, default=_REPO / "bench_results" / "attribution")
    args = ap.parse_args()

    from transcria.config import load_config, set_config

    cfg = load_config()
    _set_nested(cfg, "workflow.summary_by_turns", args.mode == "by_turns")
    for item in args.override:
        key, _, raw = item.partition("=")
        _set_nested(cfg, key.strip(), _parse_value(raw))
    set_config(cfg)

    from app import create_app
    from transcria.database import db
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.store import JobStore
    from transcria.services.job_service import JobService
    from transcria.workflow.runner import WorkflowRunner

    app = create_app(start_background_services=False)
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"label": args.label, "mode": args.mode, "audio": str(args.audio), "overrides": args.override,
                    "diarization_backend": (cfg.get("models") or {}).get("diarization_backend"),
                    "summary_stt_backend": (cfg.get("models") or {}).get("summary_stt_backend")
                    or (cfg.get("models") or {}).get("stt_backend")}

    with app.app_context():
        db.create_all()
        from transcria.auth.store import UserStore

        UserStore.ensure_admin(cfg)
        admin = UserStore.get_by_username("admin")
        if admin is None:
            raise SystemExit("compte admin introuvable")
        created = JobService.create(admin.id, f"BENCH attribution {args.label} {args.mode}")
        job_id = created["job_id"]
        jobs_dir = cfg["storage"]["jobs_dir"]
        upload = JobService.upload(job_id, args.audio.read_bytes(), args.audio.name, jobs_dir)
        if upload.get("error"):
            print(f"[ERREUR] upload : {upload['error']}", file=sys.stderr)
            return 1
        t0 = time.time()
        JobService.analyze(job_id, jobs_dir, cfg)
        report["t_analyze_s"] = round(time.time() - t0, 1)
        fs = JobFilesystem(jobs_dir, job_id)
        audio_path = fs.get_original_audio_path()
        job = JobStore.get_by_id(job_id)

        runner = WorkflowRunner(JobStore, cfg)
        t1 = time.time()
        result = runner.run_summary(job, str(audio_path), cfg)
        report["t_summary_s"] = round(time.time() - t1, 1)
        report["job_id"] = job_id
        report["summary_error"] = result.get("error")
        report["segment_count"] = result.get("segment_count", 0)

        segments = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        total_words = sum(_word_count(s.get("text", "")) for s in segments)
        labeled_words = sum(_word_count(s.get("text", "")) for s in segments if s.get("speaker"))
        report["transcript_words"] = total_words
        report["labeled_word_ratio"] = round(labeled_words / total_words, 3) if total_words else None
        report["mean_segment_s"] = round(sum(float(s.get("end", 0)) - float(s.get("start", 0)) for s in segments)
                                         / max(len(segments), 1), 1)

        turns = fs.load_json("speakers/speaker_turns.json") or {}
        report["speakers"] = turns.get("speakers") or []
        report["speaking_time_s"] = {k: round(float(v.get("speaking_time_seconds", 0)), 1)
                                     for k, v in (turns.get("stats") or {}).items()}
        ctx = fs.load_text("summary/diarization_context.md") or ""
        report["context_certain_lines"] = len(re.findall(r"^\*\*\[SPEAKER_\d+\]\*\*", ctx, flags=re.M))
        meeting = fs.load_json("context/meeting_context.json") or {}
        report["speaker_roles_llm"] = meeting.get("speaker_roles_llm") or {}
        report["participants_detectes"] = meeting.get("participants_detectes") or ""
        report["speaker_count_llm"] = len(report["speaker_roles_llm"])
        report["speaker_count_diar"] = len(report["speakers"])

        # Matière pour le jugement : par locuteur, les phrases ATTRIBUÉES les plus longues
        # (transcript étiqueté en by_turns ; lignes « certaines » du contexte sinon).
        evidence: dict[str, list[str]] = {}
        for s in segments:
            spk = s.get("speaker")
            if spk:
                evidence.setdefault(spk, []).append(s.get("text", "").strip())
        if not evidence:
            for m in re.finditer(r"^\*\*\[(SPEAKER_\d+)\]\*\*\s*(.+)$", ctx, flags=re.M):
                evidence.setdefault(m.group(1), []).append(m.group(2).strip())
        report["evidence"] = {k: sorted(v, key=len, reverse=True)[:6] for k, v in evidence.items()}

    out = out_dir / f"{args.label}_{args.mode}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {args.label} {args.mode} → {out} (résumé {report['t_summary_s']} s, "
          f"{report['speaker_count_diar']} locuteurs, couverture étiquetée {report['labeled_word_ratio']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
TranscrIA — test E2E proche du workflow de production.

Le script exécute un job réel de bout en bout avec les mêmes briques que
l'application :

1. JobService.create/upload/analyze
2. WorkflowRunner.run_summary() : STT rapide, pyannote, résumé LLM
   - pyannote détecte les locuteurs + attribut le genre par locuteur
     (gender_segments × tours → speaker_stats.json, champ "gender")
   - _write_diarization_context : section "Genre vocal par locuteur" dans
     summary/diarization_context.md
3. MeetingContextManager / ParticipantsManager / LexiconManager
4. SpeakerDetector.save_mapping() + application des rôles LLM
5. PipelineService.run_process(..., mode="quality") :
   - analyse de scène audio (subprocess librosa, pré-transcription) :
     produit metadata/audio_scene.json avec ratios, scene_segments,
     problem_segments et gender_segments horodatés
   - séparation de sources optionnelle (Demucs, pré-transcription)
   - filtrage scène optionnel : silence des zones non vocales longues sans
     décaler la timeline
   - normalisation audio optionnelle : filtres ffmpeg légers sans décaler la
     timeline
   - transcription finale (Cohere ou Whisper quality)
   - diarisation pyannote (mode quality uniquement)
   - correction LLM d'arbitrage
   - contrôle qualité
   - export ZIP

Les participants ne sont pas préremplis avec des noms fictifs. Le test crée une
entrée par SPEAKER_XX détecté et laisse la LLM appliquer les rôles/noms si elle
les déduit.

Note : arrêter et relancer le service TranscrIA avant d'exécuter ce test afin
d'éviter les conflits de port ou d'état partagé. Utiliser le Python du venv
pour que pyannote soit disponible :

    systemctl stop transcria
    venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3
    systemctl start transcria
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TRANSCRIA_CONFIG", str(Path(__file__).parent.parent / "config.yaml"))

from app import create_app
from transcria.config import load_config, set_config
from transcria.database import db


DEFAULT_AUDIO = Path(__file__).parent / "test1.mp3"
DEFAULT_JOB_TITLE = "E2E workflow production"

STEP = 0
RESULTS: dict[str, bool | None | str] = {}
TIMINGS: dict[str, float] = {}
GPU_SNAPSHOTS: list[dict] = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test E2E TranscrIA proche production")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO, help="Fichier audio à utiliser")
    parser.add_argument("--keep", action="store_true", help="Conserver le job à la fin")
    parser.add_argument("--skip-llm", action="store_true", help="Désactiver résumé/correction LLM")
    parser.add_argument("--skip-diarization", action="store_true", help="Désactiver pyannote")
    parser.add_argument("--stt-backend", choices=["cohere", "whisper"], default="cohere")
    parser.add_argument("--enable-audio-scene", action="store_true", help="Forcer workflow.audio_scene.enabled=true")
    parser.add_argument("--enable-scene-filter", action="store_true", help="Forcer le filtrage scène pré-STT")
    parser.add_argument("--enable-audio-normalization", action="store_true", help="Forcer la normalisation audio pré-STT")
    parser.add_argument("--enable-source-separation", action="store_true", help="Forcer workflow.source_separation.enabled=true")
    return parser.parse_args()


def step(name: str) -> None:
    global STEP
    STEP += 1
    print(f"\n{'=' * 70}")
    print(f"  ETAPE {STEP} : {name}")
    print(f"{'=' * 70}")


def section(title: str) -> None:
    print(f"\n{'-' * 70}")
    print(f"  {title}")
    print(f"{'-' * 70}")


def ok(message: str) -> None:
    print(f"  OK  {message}")


def warn(message: str) -> None:
    print(f"  WARN {message}")


def fail(message: str, error: object | None = None) -> None:
    print(f"  FAIL {message}")
    if error:
        print(f"       {error}")


def timer_start(name: str) -> None:
    TIMINGS[name] = time.time()


def timer_end(name: str) -> float:
    elapsed = time.time() - TIMINGS[name]
    print(f"  Temps {name}: {elapsed:.1f}s")
    TIMINGS[name] = elapsed
    return elapsed


def get_gpu_snapshot(label: str) -> dict:
    snapshot = {"label": label, "timestamp": time.time(), "gpus": [], "processes": []}
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.free,memory.total,utilization.gpu,utilization.memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                snapshot["gpus"].append(
                    {
                        "id": int(parts[0]),
                        "name": parts[1],
                        "mem_used_mb": int(parts[2]),
                        "mem_free_mb": int(parts[3]),
                        "mem_total_mb": int(parts[4]),
                        "gpu_util_pct": int(parts[5]),
                        "mem_util_pct": int(parts[6]),
                    }
                )
    except Exception as exc:
        snapshot["gpu_error"] = str(exc)

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                snapshot["processes"].append(
                    {"pid": int(parts[0]), "name": parts[1], "vram_mb": int(parts[2])}
                )
    except Exception as exc:
        snapshot["process_error"] = str(exc)

    GPU_SNAPSHOTS.append(snapshot)
    return snapshot


def print_gpu_state(snapshot: dict, prefix: str = "    ") -> None:
    gpus = snapshot.get("gpus") or []
    if not gpus:
        print(f"{prefix}nvidia-smi indisponible")
        return
    for gpu in gpus:
        print(
            f"{prefix}GPU {gpu['id']} ({gpu['name']}): "
            f"{gpu['mem_used_mb']}/{gpu['mem_total_mb']} Mo "
            f"(libre {gpu['mem_free_mb']} Mo), util {gpu['gpu_util_pct']}%"
        )
    processes = snapshot.get("processes") or []
    if processes:
        for proc in processes:
            print(f"{prefix}  PID {proc['pid']}: {proc['name']} ({proc['vram_mb']} Mo)")
    else:
        print(f"{prefix}  aucun processus GPU actif")


def gpu_checkpoint(label: str) -> dict:
    snapshot = get_gpu_snapshot(label)
    print_gpu_state(snapshot)
    return snapshot


def build_context_payload(fs, fallback_title: str) -> dict:
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    return {
        "title": meeting_ctx.get("title_suggere") or fallback_title,
        "language": "fr",
        "meeting_type": meeting_ctx.get("type_suggere") or "",
        "sensitivity": "normal",
        "objective": meeting_ctx.get("objectif_suggere") or "",
        "notes": meeting_ctx.get("notes_suggeres") or "",
    }


def build_participants_from_speakers(fs) -> list[dict]:
    speaker_stats = fs.load_json("speakers/speaker_stats.json") or {}
    speakers = speaker_stats.get("speakers") or []
    participants = []
    for idx, speaker in enumerate(speakers, start=1):
        participants.append(
            {
                "id": f"p{idx}",
                "name": "",
                "function": "",
                "service": "",
                "role": "",
                "is_animator": False,
                "expected": True,
                "comment": f"Créé automatiquement pour {speaker.get('speaker_id', f'SPEAKER_{idx:02d}')}",
            }
        )
    if not participants:
        participants.append(
            {
                "id": "p1",
                "name": "",
                "function": "",
                "service": "",
                "role": "",
                "is_animator": False,
                "expected": True,
                "comment": "Participant à identifier",
            }
        )
    return participants


def build_mapping_from_speakers(fs, participants: list[dict]) -> dict:
    speaker_stats = fs.load_json("speakers/speaker_stats.json") or {}
    speakers = speaker_stats.get("speakers") or []
    mapping = {}
    for idx, speaker in enumerate(speakers):
        speaker_id = speaker.get("speaker_id") or speaker.get("label") or f"SPEAKER_{idx:02d}"
        participant = participants[idx] if idx < len(participants) else {}
        mapping[speaker_id] = {
            "name": speaker_id,
            "participant_id": participant.get("id", ""),
        }
    return mapping


def build_lexicon_from_summary(fs) -> list[dict]:
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    lexicon = []
    for item in meeting_ctx.get("termes_suspects") or []:
        term = str(item.get("term", "")).strip()
        if not term:
            continue
        lexicon.append(
            {
                "term": term,
                "category": item.get("category", "terme_metier"),
                "priority": item.get("priority", "normale"),
                "variants": item.get("variants", []),
                "replace_by": "",
                "comment": item.get("comment", ""),
                "contexts": item.get("contexts", []),
            }
        )
    return lexicon


def assert_file(path: Path, label: str) -> bool:
    if path.exists() and path.stat().st_size > 0:
        ok(f"{label}: {path.relative_to(path.parents[1])} ({path.stat().st_size} octets)")
        return True
    warn(f"{label} absent ou vide: {path}")
    return False


def print_json_artifact(fs, relative_path: str, label: str) -> dict | list | None:
    path = fs.job_dir / relative_path
    if not assert_file(path, label):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warn(f"{label}: JSON illisible ({exc})")
        return None
    if isinstance(data, dict):
        preview_keys = ", ".join(list(data.keys())[:8])
        print(f"    clés: {preview_keys}")
    return data


def apply_e2e_feature_flags(cfg: dict, args: argparse.Namespace) -> None:
    workflow = cfg.setdefault("workflow", {})
    if args.enable_audio_scene or args.enable_scene_filter:
        workflow.setdefault("audio_scene", {})["enabled"] = True
    if args.enable_scene_filter:
        workflow.setdefault("audio_scene_filter", {})["enabled"] = True
        workflow.setdefault("audio_scene_filter", {}).setdefault("enabled_for_modes", ["quality"])
    if args.enable_audio_normalization:
        workflow.setdefault("audio_normalization", {})["enabled"] = True
        workflow.setdefault("audio_normalization", {}).setdefault("enabled_for_modes", ["quality"])
    if args.enable_source_separation:
        workflow.setdefault("source_separation", {})["enabled"] = True


def print_preprocessing_config(cfg: dict) -> None:
    workflow = cfg.get("workflow", {})
    items = [
        ("audio_scene", workflow.get("audio_scene", {}).get("enabled", False)),
        ("source_separation", workflow.get("source_separation", {}).get("enabled", False)),
        ("audio_scene_filter", workflow.get("audio_scene_filter", {}).get("enabled", False)),
        ("audio_normalization", workflow.get("audio_normalization", {}).get("enabled", False)),
    ]
    for name, enabled in items:
        print(f"  {name:22s}: {'oui' if enabled else 'non'}")


def print_summary() -> None:
    section("RESUME FINAL")
    print(f"  Job ID : {RESULTS.get('job_id', 'N/A')}")
    print(f"  Etapes : {STEP}")
    print("\n  GPU timeline:")
    for snapshot in GPU_SNAPSHOTS:
        first_gpu = (snapshot.get("gpus") or [None])[0]
        if first_gpu:
            print(
                f"    {snapshot['label'][:42]:42s} "
                f"GPU0 {first_gpu['mem_used_mb']:>6} Mo utilises, "
                f"{len(snapshot.get('processes') or []):>2} proc"
            )
        else:
            print(f"    {snapshot['label'][:42]:42s} nvidia-smi indisponible")
    print("\n  Temps:")
    for name, elapsed in TIMINGS.items():
        if elapsed > 1_000_000_000:
            elapsed = time.time() - elapsed
        print(f"    {name:28s} {elapsed:.1f}s")

    failed = [k for k, v in RESULTS.items() if v is False]
    skipped = [k for k, v in RESULTS.items() if v is None]
    ok_count = sum(1 for v in RESULTS.values() if v is True)
    print(f"\n  Resultats : {ok_count} OK / {len(failed)} echec(s) / {len(skipped)} ignore(s)")
    if failed:
        print(f"  Echecs : {failed}")


def main() -> int:
    args = parse_args()
    audio_file = args.audio
    if not audio_file.exists():
        fail(f"Fichier audio introuvable: {audio_file}")
        return 1

    print(f"\n{'#' * 70}")
    print("  TranscrIA — E2E workflow production")
    print(f"  Audio : {audio_file} ({audio_file.stat().st_size / 1024:.0f} Ko)")
    print(f"  STT   : {args.stt_backend}")
    print(f"  LLM   : {'non' if args.skip_llm else 'oui'}")
    print(f"  Diar  : {'non' if args.skip_diarization else 'oui'}")
    print("  Options pré-STT forcées :")
    print(f"    audio_scene          : {'oui' if args.enable_audio_scene else 'config'}")
    print(f"    source_separation    : {'oui' if args.enable_source_separation else 'config'}")
    print(f"    audio_scene_filter   : {'oui' if args.enable_scene_filter else 'config'}")
    print(f"    audio_normalization  : {'oui' if args.enable_audio_normalization else 'config'}")
    print(f"{'#' * 70}")

    section("Etat initial GPU")
    gpu_checkpoint("initial")

    step("Initialisation Flask / DB")
    timer_start("init")
    cfg = load_config()
    cfg.setdefault("models", {})["stt_backend"] = args.stt_backend
    apply_e2e_feature_flags(cfg, args)
    if args.skip_llm:
        cfg.setdefault("workflow", {}).setdefault("summary_llm", {})["enabled"] = False
        cfg.setdefault("workflow", {}).setdefault("arbitration_llm", {})["enabled"] = False
    if args.skip_diarization:
        cfg.setdefault("workflow", {})["enable_speaker_detection"] = False
    set_config(cfg)
    section("Configuration pré-STT effective")
    print_preprocessing_config(cfg)

    app = create_app()
    app.config.update({"TESTING": True})
    with app.app_context():
        db.create_all()
        from transcria.auth.store import UserStore

        UserStore.ensure_admin(cfg)
        admin = UserStore.get_by_username("admin")
        if admin is None:
            raise RuntimeError("Utilisateur admin introuvable")
        ok("Application et admin prets")
    RESULTS["init"] = True
    timer_end("init")

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.context.job_context_builder import JobContextBuilder
        from transcria.context.lexicon import LexiconManager
        from transcria.context.meeting_context import MeetingContextManager
        from transcria.context.participants import ParticipantsManager
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.models import JobState
        from transcria.jobs.store import JobStore
        from transcria.services.job_service import JobService
        from transcria.services.pipeline_service import PipelineService
        from transcria.stt.speaker_detection import SpeakerDetector
        from transcria.workflow.runner import WorkflowRunner
        from transcria.workflow.transitions import advance_preprocessing_state

        admin = UserStore.get_by_username("admin")

        step("Creation / upload / analyse via JobService")
        timer_start("prepare")
        created = JobService.create(admin.id, DEFAULT_JOB_TITLE)
        job_id = created["job_id"]
        RESULTS["job_id"] = job_id
        ok(f"Job cree: {job_id}")

        upload = JobService.upload(job_id, audio_file.read_bytes(), audio_file.name, cfg["storage"]["jobs_dir"])
        ok(f"Upload: {upload.get('original_filename')} ({upload.get('size_bytes')} octets)")

        analysis = JobService.analyze(job_id, cfg["storage"]["jobs_dir"], cfg)
        if analysis.get("error"):
            raise RuntimeError(analysis["error"])
        ok(f"Analyse: {analysis.get('duration_seconds', 0):.1f}s, codec={analysis.get('codec')}")
        job = JobStore.get_by_id(job_id)
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job_id)
        audio_path = fs.get_original_audio_path()
        if audio_path is None:
            raise RuntimeError("Audio original introuvable apres upload")
        RESULTS["prepare"] = True
        timer_end("prepare")

        step("Resume production via WorkflowRunner.run_summary")
        timer_start("summary")
        gpu_checkpoint("avant summary")
        runner = WorkflowRunner(JobStore, cfg)
        summary_result = runner.run_summary(job, str(audio_path), cfg)
        gpu_checkpoint("apres summary")
        if summary_result.get("error") and not summary_result.get("transcript_text"):
            raise RuntimeError(summary_result["error"])
        job = JobStore.get_by_id(job_id)
        ok(f"Etat apres summary: {job.state}")
        ok(f"Transcription rapide: {summary_result.get('segment_count', 0)} segment(s)")
        if not args.skip_llm:
            summary_md = fs.job_dir / "summary" / "summary.md"
            assert_file(summary_md, "summary.md")
        RESULTS["summary"] = True
        timer_end("summary")

        step("Contexte utilisateur")
        timer_start("context")
        context_payload = build_context_payload(fs, DEFAULT_JOB_TITLE)
        MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], context_payload)
        if job.state == JobState.SUMMARY_DONE.value:
            JobStore.update_state(job.id, JobState.CONTEXT_DONE)
        job = JobStore.get_by_id(job_id)
        ok(f"Contexte sauvegarde, etat={job.state}")
        RESULTS["context"] = True
        timer_end("context")

        step("Participants sans noms preinjectes")
        timer_start("participants")
        participants = build_participants_from_speakers(fs)
        saved_participants = ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], participants)
        if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
            JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
        job = JobStore.get_by_id(job_id)
        ok(f"Participants crees: {len(saved_participants)} (noms vides volontairement)")
        RESULTS["participants"] = True
        timer_end("participants")

        step("Lexique utilisateur")
        timer_start("lexicon")
        lexicon = build_lexicon_from_summary(fs)
        LexiconManager.save(job, cfg["storage"]["jobs_dir"], lexicon)
        advance_preprocessing_state(job.id, job.state)
        JobContextBuilder.build(job, cfg["storage"]["jobs_dir"], cfg)
        job = JobStore.get_by_id(job_id)
        ok(f"Lexique sauvegarde: {len(lexicon)} terme(s), etat={job.state}")
        RESULTS["lexicon"] = True
        timer_end("lexicon")

        step("Mapping SPEAKER_XX sans noms humains injectes")
        timer_start("mapping")
        participants = ParticipantsManager.get(job, cfg["storage"]["jobs_dir"])
        mapping = build_mapping_from_speakers(fs, participants)
        if mapping:
            SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
            JobContextBuilder.build(job, cfg["storage"]["jobs_dir"], cfg)
            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
            speaker_roles_llm = meeting_ctx.get("speaker_roles_llm", {})
            if speaker_roles_llm:
                import logging

                WorkflowRunner._apply_speaker_roles(
                    fs,
                    speaker_roles_llm,
                    logging.getLogger("e2e.summary"),
                )
                ok(f"Roles LLM appliques: {list(speaker_roles_llm.keys())}")
            ok(f"Mapping sauvegarde: {len(mapping)} speaker(s)")
        else:
            warn("Aucun SPEAKER_XX detecte, mapping ignore")
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
        job = JobStore.get_by_id(job_id)
        RESULTS["mapping"] = True
        timer_end("mapping")

        step("Traitement complet")
        timer_start("pipeline")
        gpu_checkpoint("avant pipeline")
        mode = "quality" if cfg.get("workflow", {}).get("enable_quality_mode", True) else "fast"
        # PipelineService gère le cycle complet : analyse de scène, séparation
        # optionnelle, transcription, diarisation (mode quality), correction,
        # qualité, export. Quand --skip-llm, les LLM sont déjà désactivés dans
        # cfg (lignes ci-dessus) : la correction est sautée automatiquement.
        pipeline = PipelineService(cfg)
        result = pipeline.run_process(job, str(audio_path), mode)
        gpu_checkpoint("apres pipeline")
        if result.get("error"):
            raise RuntimeError(f"{result.get('step')}: {result['error']}")
        job = JobStore.get_by_id(job_id)
        ok(f"Pipeline termine, etat={job.state}, mode={mode}")
        RESULTS["pipeline"] = True
        timer_end("pipeline")

        step("Verification artefacts")
        timer_start("verify")
        expected = [
            "input/original.mp3",
            "metadata/audio_analysis.json",
            "summary/quick_transcript.txt",
            "summary/summary.json",
            "summary/summary.md",
            "context/meeting_context.json",
            "context/participants.json",
            "context/session_lexicon.json",
            "context/job_context.yaml",
            "metadata/transcription.srt",
            "quality/quality_report.json",
        ]
        if mapping:
            expected.extend(
                [
                    "speakers/speaker_stats.json",
                    "speakers/speaker_mapping.json",
                ]
            )
        if not args.skip_llm:
            expected.extend(
                [
                    "metadata/transcription_corrigee.srt",
                    "metadata/correction_report.md",
                ]
            )
        found = 0
        for rel_path in expected:
            if assert_file(fs.job_dir / rel_path, rel_path):
                found += 1
        exports = list((fs.job_dir / "exports").glob("*.zip"))
        if exports:
            ok(f"Export ZIP: {exports[0].name} ({exports[0].stat().st_size} octets)")
            found += 1
        else:
            warn("Export ZIP absent")
        RESULTS["verify"] = found == len(expected) + 1
        timer_end("verify")

        section("Artefacts optionnels (selon config)")
        optional_artifacts = [
            ("metadata/audio_quality_decision.json", "Décision qualité audio"),
            ("metadata/audio_scene.json", "Analyse de scène audio"),
            ("metadata/audio_scene_filter.json", "Filtrage scène audio"),
            ("metadata/audio_normalization.json", "Normalisation audio"),
            ("input/vocals.wav", "Piste vocale séparée"),
            ("input/scene_filtered.wav", "Audio filtré scène"),
            ("input/normalized.wav", "Audio normalisé"),
            ("speakers/diarization_checkpoint.json", "Checkpoint diarisation"),
            ("speakers/speaker_embeddings.json", "Embeddings locuteurs"),
            ("summary/diarization_context.md", "Contexte diarisation LLM"),
        ]
        for rel_path, label in optional_artifacts:
            assert_file(fs.job_dir / rel_path, label)

        section("Prétraitements audio appliqués")
        scene_data = print_json_artifact(fs, "metadata/audio_scene.json", "audio_scene.json")
        if isinstance(scene_data, dict):
            ok(
                "audio_scene: "
                f"speech_ratio={scene_data.get('speech_ratio')} "
                f"music_ratio={scene_data.get('music_ratio')} "
                f"noise_ratio={scene_data.get('noise_ratio')} "
                f"problem_segments={len(scene_data.get('problem_segments') or [])}"
            )
        filter_data = print_json_artifact(fs, "metadata/audio_scene_filter.json", "audio_scene_filter.json")
        if isinstance(filter_data, dict):
            if filter_data.get("preserve_timeline") is True:
                ok(f"audio_scene_filter: timeline préservée, {len(filter_data.get('intervals') or [])} intervalle(s)")
            else:
                RESULTS["audio_scene_filter_timeline"] = False
                fail("audio_scene_filter: preserve_timeline absent ou false")
        elif args.enable_scene_filter:
            warn("audio_scene_filter forcé mais aucun filtrage appliqué (souvent faute de zones filtrables)")

        normalization_data = print_json_artifact(fs, "metadata/audio_normalization.json", "audio_normalization.json")
        if isinstance(normalization_data, dict):
            if normalization_data.get("preserve_timeline") is True:
                ok(f"audio_normalization: timeline préservée, filtres={normalization_data.get('filters')}")
            else:
                RESULTS["audio_normalization_timeline"] = False
                fail("audio_normalization: preserve_timeline absent ou false")
        elif args.enable_audio_normalization:
            RESULTS["audio_normalization"] = False
            fail("audio_normalization forcée mais metadata/audio_normalization.json absent")

        # Vérification genre par locuteur (attribution acoustique automatique)
        section("Genre vocal par locuteur (attribution acoustique)")
        scene_path = fs.job_dir / "metadata" / "audio_scene.json"
        if scene_path.exists():
            scene = json.loads(scene_path.read_text())
            gender_segs = scene.get("gender_segments") or []
            print(f"  gender_segments dans audio_scene.json : {len(gender_segs)} segment(s)")
            if gender_segs:
                total_female = sum(s["end"] - s["start"] for s in gender_segs if s.get("label") == "female")
                total_male = sum(s["end"] - s["start"] for s in gender_segs if s.get("label") == "male")
                ok(f"Segments genre : {total_female:.1f}s féminin / {total_male:.1f}s masculin")
            else:
                warn("Pas de segments genre horodatés (detect_gender désactivé ou audio trop court)")
        else:
            warn("audio_scene.json absent — analyse de scène désactivée ou échouée")

        stats_data = fs.load_json("speakers/speaker_stats.json") or {}
        spk_list = stats_data.get("speakers") or []
        gender_filled = [s for s in spk_list if s.get("gender")]
        print(f"  Locuteurs avec genre attribué : {len(gender_filled)}/{len(spk_list)}")
        for spk in spk_list:
            spk_id = spk.get("speaker_id", "?")
            gender = spk.get("gender") or "(non attribué)"
            mapped = spk.get("mapped_name") or ""
            label = f"{mapped} ({spk_id})" if mapped and not mapped.upper().startswith("SPEAKER_") else spk_id
            print(f"    {label:30s}  genre={gender}")

        section("Participants finaux")
        for participant in ParticipantsManager.get(job, cfg["storage"]["jobs_dir"]):
            print(
                "  "
                + json.dumps(
                    {
                        "id": participant.get("id"),
                        "name": participant.get("name"),
                        "role": participant.get("role"),
                    },
                    ensure_ascii=False,
                )
            )

        if args.keep:
            section("Job conserve")
            print(f"  Job ID     : {job_id}")
            print(f"  Repertoire : {fs.job_dir}")
        else:
            section("Nettoyage")
            shutil.rmtree(fs.job_dir, ignore_errors=True)
            ok(f"Job supprime: {fs.job_dir}")

    print_summary()
    failed = [k for k, v in RESULTS.items() if v is False]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        fail("E2E interrompu", exc)
        RESULTS["e2e"] = False
        traceback.print_exc()
        print_summary()
        raise SystemExit(1)

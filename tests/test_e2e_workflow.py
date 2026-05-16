#!/usr/bin/env python3
"""
TranscrIA — Test d'intégration E2E avec points de contrôle GPU/VRAM.

À chaque étape GPU (Cohere, pyannote, Qwen résumé, Qwen correction),
le script capture l'état des GPUs avant/après via nvidia-smi et
vérifie qu'une activité GPU a bien eu lieu.

Utilisation :
    python tests/test_e2e_workflow.py [--keep] [--skip-llm] [--skip-diarization]
                                      [--stt-backend cohere|whisper]
                                      [--test-services] [--test-new-components]

Options :
    --keep                  Ne pas supprimer le job à la fin
    --skip-llm              Sauter les étapes LLM (Qwen résumé + correction)
    --skip-diarization      Sauter la diarisation pyannote
    --stt-backend BACKEND   Choix du moteur STT (cohere, whisper)
    --test-services         Tester via le Service Layer (JobService, PipelineService)
    --test-new-components   Tester GPUSession, TranscriberFactory, LLMBackend
"""

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TRANSCRIA_CONFIG", str(Path(__file__).parent.parent / "config.yaml"))

from transcria.config import load_config, set_config
from transcria.database import db
from app import create_app

AUDIO_FILE = Path(__file__).parent / "test1.mp3"
TEST_JOB_TITLE = "E2E GPU Checkpoint"
KEEP_JOB = "--keep" in sys.argv
SKIP_LLM = "--skip-llm" in sys.argv
SKIP_DIAR = "--skip-diarization" in sys.argv
TEST_SERVICES = "--test-services" in sys.argv
TEST_NEW_COMPONENTS = "--test-new-components" in sys.argv
STT_BACKEND = "cohere"
for i, a in enumerate(sys.argv):
    if a.startswith("--stt-backend="):
        STT_BACKEND = a.split("=", 1)[1]
    elif a == "--stt-backend" and i + 1 < len(sys.argv):
        STT_BACKEND = sys.argv[i + 1]

STEP = 0
RESULTS = {}
TIMINGS = {}
GPU_SNAPSHOTS = []


def get_gpu_snapshot(label):
    snapshot = {"label": label, "timestamp": time.time()}
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.used,memory.free,memory.total,utilization.gpu,utilization.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        gpus = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "id": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mb": int(parts[2]),
                    "mem_free_mb": int(parts[3]),
                    "mem_total_mb": int(parts[4]),
                    "gpu_util_pct": int(parts[5]),
                    "mem_util_pct": int(parts[6]),
                })
            elif len(parts) >= 4:
                gpus.append({
                    "id": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mb": int(parts[2]),
                    "mem_free_mb": int(parts[3]),
                    "mem_total_mb": 0,
                    "gpu_util_pct": 0,
                    "mem_util_pct": 0,
                })
        snapshot["gpus"] = gpus
    except Exception:
        snapshot["gpus"] = []
    # Also check for running compute processes
    try:
        result2 = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        procs = []
        for line in result2.stdout.strip().split("\n"):
            if line.strip():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    procs.append({"pid": int(parts[0]), "name": parts[1], "vram_mb": int(parts[2])})
        snapshot["processes"] = procs
    except Exception:
        snapshot["processes"] = []
    GPU_SNAPSHOTS.append(snapshot)
    return snapshot


def print_gpu_state(snapshot, prefix=""):
    gpus = snapshot.get("gpus", [])
    procs = snapshot.get("processes", [])
    if not gpus:
        print(f"{prefix}⚠️  nvidia-smi indisponible")
        return
    for g in gpus:
        print(f"{prefix}GPU {g['id']} ({g['name']}): "
              f"VRAM {g['mem_used_mb']}/{g['mem_total_mb']} Mo "
              f"(libre {g['mem_free_mb']} Mo) | "
              f"Util GPU {g.get('gpu_util_pct', '?')}% | "
              f"Util Mem {g.get('mem_util_pct', '?')}%")
    if procs:
        for p in procs:
            print(f"{prefix}  ↳ PID {p['pid']}: {p['name']} ({p['vram_mb']} Mo)")
    else:
        print(f"{prefix}  (aucun processus GPU actif)")


def verify_gpu_activity(before, after, model_name, expected_min_vram_mb=500):
    """Vérifie qu'une activité GPU a eu lieu entre before et after."""
    if not before.get("gpus") or not after.get("gpus"):
        warn(f"Impossible de vérifier l'activité GPU pour {model_name} (nvidia-smi indisponible)")
        return

    found_activity = False
    for bg in before["gpus"]:
        for ag in after["gpus"]:
            if bg["id"] == ag["id"]:
                vram_delta = ag["mem_used_mb"] - bg["mem_used_mb"]
                if vram_delta >= expected_min_vram_mb:
                    found_activity = True
                    ok(f"GPU {bg['id']}: +{vram_delta} Mo VRAM utilisé par {model_name} "
                       f"({bg['mem_used_mb']}→{ag['mem_used_mb']} Mo)")
                elif vram_delta > 0:
                    found_activity = True
                    ok(f"GPU {bg['id']}: +{vram_delta} Mo VRAM par {model_name} (faible, attendu ≥{expected_min_vram_mb} Mo)")

    # Check processes
    model_keywords = {
        "Cohere": "python",
        "pyannote": "python",
        "Qwen résumé": ["llama-server", "python", "opencode"],
        "Qwen correction": ["llama-server", "python", "opencode"],
    }
    keywords = model_keywords.get(model_name, ["python"])
    if isinstance(keywords, str):
        keywords = [keywords]

    for p in after.get("processes", []):
        for kw in keywords:
            if kw.lower() in p["name"].lower():
                found_activity = True
                ok(f"Processus GPU détecté : PID {p['pid']} ({p['name']}, {p['vram_mb']} Mo)")

    if not found_activity and not SKIP_LLM:
        warn(f"Aucune activité GPU détectée pour {model_name} — vérifiez que le modèle s'est chargé")


def step(name):
    global STEP
    STEP += 1
    print(f"\n{'='*70}")
    print(f"  ÉTAPE {STEP} : {name}")
    print(f"{'='*70}")


def ok(msg):
    print(f"  ✅ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def fail(msg, error=None):
    print(f"  ❌ {msg}")
    if error:
        print(f"      {error}")


def timer_start(name):
    TIMINGS[name] = time.time()


def timer_end(name):
    elapsed = time.time() - TIMINGS[name]
    print(f"  ⏱  {name}: {elapsed:.1f}s")
    return elapsed


def section(title):
    print(f"\n{'─'*70}")
    print(f"  {title}")
    print(f"{'─'*70}")


def gpu_checkpoint(label):
    snap = get_gpu_snapshot(label)
    print_gpu_state(snap, prefix="    ")
    return snap


def print_summary():
    section("RÉSUMÉ FINAL — GPU / VRAM")
    print(f"\n  Fichier audio : {AUDIO_FILE}")
    print(f"  Job ID : {RESULTS.get('job_id', 'N/A')}")
    print(f"  Étapes : {STEP}")

    section("Timeline GPU")
    if GPU_SNAPSHOTS:
        # Print a compact timeline
        print(f"\n  {'Étape':40s} {'GPU0 Used':>10s} {'GPU0 Free':>10s} {'Procs':>6s}")
        print(f"  {'─'*70}")
        for snap in GPU_SNAPSHOTS:
            gpu0 = next((g for g in snap["gpus"] if g["id"] == 0), None)
            nprocs = len(snap.get("processes", []))
            used = f"{gpu0['mem_used_mb']} Mo" if gpu0 else "N/A"
            free = f"{gpu0['mem_free_mb']} Mo" if gpu0 else "N/A"
            print(f"  {snap['label']:40s} {used:>10s} {free:>10s} {nprocs:>6d}")
    else:
        print("  Aucun snapshot GPU capturé")

    section("Temps par étape")
    for name in sorted(TIMINGS.keys()):
        elapsed = time.time() - TIMINGS[name]
        print(f"    {name:40s} {elapsed:.1f}s")

    ok_tests = sum(1 for v in RESULTS.values() if v is True)
    skip_tests = sum(1 for v in RESULTS.values() if v is None)
    fail_tests = sum(1 for v in RESULTS.values() if v is False)
    total = len(RESULTS)
    print(f"\n  Résultats : {ok_tests} réussis / {fail_tests} échoués / {skip_tests} ignorés / {total} total")

    if fail_tests == 0:
        print(f"\n  🎉 Tous les tests E2E sont passés !")
    else:
        print(f"\n  ❌ {fail_tests} test(s) échoué(s). Voir les détails ci-dessus.")


# ═══════════════════════════════════════════════════════════════════
#  Tests des nouveaux composants (Phases 2-4)
# ═══════════════════════════════════════════════════════════════════

def _test_gpu_session(cfg):
    step("Test GPUSession (context manager)")
    timer_start("gpu_session")
    try:
        from transcria.gpu.vram_manager import VRAMManager
        from transcria.gpu.gpu_session import GPUSession, GPUSessionError

        vram = VRAMManager(config=cfg)
        gpu_before = gpu_checkpoint("AVANT GPUSession")
        gpu_during = gpu_before
        try:
            with GPUSession(vram, "test-session", vram.cohere_vram_mb) as gs:
                ok(f"GPUSession alloué: GPU {gs.gpu_index} ({vram.cohere_vram_mb} Mo)")
                gpu_during = gpu_checkpoint("PENDANT GPUSession")
                verify_gpu_activity(gpu_before, gpu_during, "GPUSession", expected_min_vram_mb=500)
        except GPUSessionError as exc:
            warn(f"GPUSession refusée (VRAM probablement insuffisante): {exc}")
        gpu_after = gpu_checkpoint("APRÈS GPUSession (libéré)")
        verify_gpu_activity(gpu_during, gpu_after, "GPUSession libération",
                           expected_min_vram_mb=-10000)
        RESULTS["gpu_session"] = True
    except Exception as e:
        fail("Échec GPUSession", str(e))
        RESULTS["gpu_session"] = False
        traceback.print_exc()
    timer_end("gpu_session")


def _test_llm_backend(cfg):
    step("Test LLMBackend factory")
    timer_start("llm_backend")
    try:
        from transcria.gpu.llm_backend import (
            create_llm_backend, ScriptLLMBackend, OllamaLLMBackend, HTTPLLMBackend
        )

        backend = create_llm_backend(cfg)
        ok(f"Backend LLM détecté: {backend.backend_type} "
           f"(model={backend.model_id}, port={backend.port})")

        services = cfg.get("services", {})
        if services.get("ollama_url"):
            b2 = create_llm_backend(cfg, backend_type="ollama")
            ok(f"Ollama backend créé: {b2.backend_type}")
            b2.is_available()
            ok(f"Ollama available: {b2.is_available()}")

        b3 = create_llm_backend(cfg, backend_type="http")
        ok(f"HTTP backend créé: {b3.backend_type} (base_url={b3.base_url})")

        RESULTS["llm_backend"] = True
    except Exception as e:
        fail("Échec LLMBackend", str(e))
        RESULTS["llm_backend"] = False
        traceback.print_exc()
    timer_end("llm_backend")


def _test_transcriber_factory(cfg, fs):
    step("Test TranscriberFactory")
    timer_start("transcriber_factory")
    try:
        from transcria.stt.transcriber_factory import (
            create_transcriber, list_available_backends, get_backend_vram_mb
        )

        backends = list_available_backends()
        ok(f"Backends STT disponibles: {backends}")

        for b in backends:
            vram = get_backend_vram_mb(b, cfg)
            ok(f"  {b}: ~{vram} Mo VRAM")

        transcriber = create_transcriber(cfg, backend=STT_BACKEND)
        ok(f"Transcriber créé: {transcriber.model_name} (available={transcriber.available})")
        ok(f"  VRAM: {transcriber.vram_mb} Mo, Langues: {len(transcriber.supported_languages)}")

        if transcriber.available and STT_BACKEND == "whisper" and not SKIP_DIAR:
            ok("Test chargement + transcription rapide whisper...")
            loaded = transcriber.load()
            if loaded:
                audio_path = fs.job_dir / "input" / "original.mp3"
                if audio_path.exists():
                    segs = transcriber.transcribe(audio_path, language="fr")
                    ok(f"  Segments: {len(segs)}")
                    srt = transcriber.segments_to_srt(segs)
                    ok(f"  SRT produit: {len(srt)} caractères")
                transcriber.offload()

        RESULTS["transcriber_factory"] = True
    except Exception as e:
        fail("Échec TranscriberFactory", str(e))
        RESULTS["transcriber_factory"] = False
        traceback.print_exc()
    timer_end("transcriber_factory")


def _test_summary_via_runner(job, audio_path, cfg):
    step("Test Summary via WorkflowRunner (run_cohere_transcription)")
    timer_start("runner_cohere")
    try:
        from transcria.workflow.runner import WorkflowRunner
        from transcria.jobs.store import JobStore
        from transcria.logging_setup import get_structured_logger

        runner = WorkflowRunner(JobStore, cfg)
        sl = get_structured_logger("e2e.test")
        sl.set_context(job_id=job.id, step="runner_cohere")

        gpu_before = gpu_checkpoint("AVANT runner cohere")
        result = runner._run_cohere_transcription(job, audio_path, cfg, sl)
        gpu_after = gpu_checkpoint("APRÈS runner cohere")
        verify_gpu_activity(gpu_before, gpu_after, "Runner Cohere", expected_min_vram_mb=500)

        ok(f"Transcription via runner: {result.get('segment_count', 0)} segments")
        ok(f"Texte: {(result.get('transcript_text', '') or '')[:80]}...")

        RESULTS["runner_cohere"] = True
    except Exception as e:
        fail("Échec runner cohere", str(e))
        RESULTS["runner_cohere"] = False
        traceback.print_exc()
    timer_end("runner_cohere")


def _test_job_service(cfg):
    step("Test JobService (upload + analyze)")
    timer_start("job_service")
    try:
        from transcria.services.job_service import JobService
        from transcria.auth.store import UserStore

        admin = UserStore.get_by_username("admin")
        jr = JobService.create(admin.id, "E2E JobService Test")
        job_id = jr["job_id"]
        ok(f"JobService job créé: {job_id}")

        upload_result = JobService.upload(
            job_id, AUDIO_FILE.read_bytes(), AUDIO_FILE.name, cfg["storage"]["jobs_dir"]
        )
        ok(f"Upload via service: {upload_result.get('original_filename', '?')}")

        analyze_result = JobService.analyze(job_id, cfg["storage"]["jobs_dir"], cfg)
        ok(f"Analyze via service: {analyze_result.get('duration_seconds', 0):.1f}s")

        JobService.delete(job_id, cfg["storage"]["jobs_dir"])
        ok("JobService job supprimé")
        RESULTS["job_service"] = True
    except Exception as e:
        fail("Échec JobService", str(e))
        RESULTS["job_service"] = False
        traceback.print_exc()
    timer_end("job_service")


def _test_pipeline_service(job, audio_path, cfg):
    step("Test PipelineService")
    timer_start("pipeline_service")
    try:
        from transcria.services.pipeline_service import PipelineService
        from transcria.jobs.store import JobStore

        pipeline = PipelineService(cfg)
        ok("PipelineService initialisé")

        steps = pipeline._define_pipeline_steps(job, audio_path, "fast")
        ok(f"Étapes pipeline fast: {[s['name'] for s in steps]}")

        quality_steps = pipeline._define_pipeline_steps(job, audio_path, "quality")
        ok(f"Étapes pipeline quality: {[s['name'] for s in quality_steps]}")

        RESULTS["pipeline_service"] = True
    except Exception as e:
        fail("Échec PipelineService", str(e))
        RESULTS["pipeline_service"] = False
        traceback.print_exc()
    timer_end("pipeline_service")


# ─── Main ─────────────────────────────────────────────────────

def main():
    if not AUDIO_FILE.exists():
        fail(f"Fichier audio introuvable : {AUDIO_FILE}")
        sys.exit(1)

    print(f"\n{'#'*70}")
    print(f"  TranscrIA — Test E2E avec contrôle GPU/VRAM")
    print(f"  Fichier : {AUDIO_FILE.name} ({AUDIO_FILE.stat().st_size / 1024:.0f} Ko)")
    if SKIP_LLM:
        print(f"  Mode : SANS LLM (--skip-llm)")
    if SKIP_DIAR:
        print(f"  Mode : SANS diarisation (--skip-diarization)")
    print(f"{'#'*70}")

    # ── Snapshot GPU initial ──
    section("État initial des GPUs")
    gpu_before_all = gpu_checkpoint("État initial")

    # ── Init ──
    step("Initialisation")
    timer_start("init")
    try:
        cfg = load_config()
        if STT_BACKEND != "cohere":
            cfg.setdefault("models", {})["stt_backend"] = STT_BACKEND
        set_config(cfg)
        app = create_app()
        app.config.update({"TESTING": True})
        ok(f"Application Flask initialisée")

        with app.app_context():
            db.create_all()
            from transcria.auth.store import UserStore
            from transcria.auth.models import Role
            from transcria.jobs.models import JobState as JS
            UserStore.ensure_admin(cfg)
            ok("DB + admin OK")
        RESULTS["init"] = True
    except Exception as e:
        fail("Échec initialisation", str(e))
        RESULTS["init"] = False
        sys.exit(1)
    timer_end("init")

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.auth.models import Role
        from transcria.jobs.store import JobStore
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.models import JobState as JS
        from transcria.audio.analyzer import AudioAnalyzer
        from transcria.audio.converter import AudioConverter
        from transcria.workflow.runner import WorkflowRunner
        from transcria.stt.transcriber_factory import create_transcriber, get_backend_vram_mb
        from transcria.stt.diarization import DiarizerService
        from transcria.gpu.vram_manager import VRAMManager
        from transcria.context.meeting_context import MeetingContextManager
        from transcria.context.participants import ParticipantsManager
        from transcria.context.lexicon import LexiconManager
        from transcria.context.job_context_builder import JobContextBuilder
        from transcria.stt.speaker_detection import SpeakerDetector
        from transcria.stt.transcription import Transcriber
        from transcria.stt.summary import SummaryGenerator
        from transcria.quality.quality_report import QualityReporter
        from transcria.exports.package_builder import PackageBuilder

        admin = UserStore.get_by_username("admin")
        vram = VRAMManager(config=cfg)

        # ── 1. Créer job ──
        step("Création du job")
        timer_start("create_job")
        job = JobStore.create_job(admin.id, TEST_JOB_TITLE)
        ok(f"Job créé : {job.id}")
        RESULTS["job_id"] = job.id
        RESULTS["create_job"] = True
        timer_end("create_job")

        # ── 2. Upload ──
        step("Upload audio")
        timer_start("upload")
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        upload_result = fs.save_upload(AUDIO_FILE.read_bytes(), AUDIO_FILE.name)
        ok(f"Fichier : {upload_result['original_filename']} ({upload_result['size_bytes']} octets)")
        JobStore.update_state(job.id, JS.UPLOADED)
        job = JobStore.get_by_id(job.id)
        ok(f"État : {job.state}")
        RESULTS["upload"] = True
        timer_end("upload")

        # ── 3. Analyse ──
        step("Analyse audio (ffprobe)")
        timer_start("analyze")
        audio_path = fs.get_original_audio_path()
        analysis = AudioAnalyzer.analyze(audio_path)
        fs.save_json("metadata/audio_analysis.json", analysis)
        JobStore.update_state(job.id, JS.ANALYZED)
        ok(f"Durée : {analysis['duration_seconds']:.1f}s | Codec : {analysis['codec']} | {analysis['channels']}ch | {analysis['sample_rate_hz']} Hz")
        ok(f"Conversion nécessaire : {analysis.get('needs_conversion', False)}")
        RESULTS["analyze"] = True
        timer_end("analyze")

        # ── 3b. Conversion ──
        if analysis.get("needs_conversion"):
            step("Conversion WAV mono 16kHz")
            timer_start("convert")
            wav_path = fs.job_dir / "input" / "audio_converted.wav"
            success = AudioConverter.convert_to_wav_mono_16k(audio_path, wav_path)
            if success and wav_path.exists():
                ok(f"Convertis : {wav_path.name} ({wav_path.stat().st_size / 1024:.0f} Ko)")
                audio_path = wav_path
                RESULTS["convert"] = True
            else:
                warn("Conversion échouée")
                RESULTS["convert"] = False
            timer_end("convert")

        # ─────────────────────────────────────────────────────────────
        #  4. TRANSCRIPTION STT — GPU CHECKPOINT
        # ─────────────────────────────────────────────────────────────
        step(f"Transcription STT ({STT_BACKEND})")
        timer_start("stt_asr")
        gpu_before_stt = gpu_checkpoint(f"AVANT {STT_BACKEND} ASR")

        try:
            from transcria.stt.transcriber_factory import get_backend_vram_mb
            vram_needed = get_backend_vram_mb(STT_BACKEND, cfg)

            gpu = vram.ensure_free(vram_needed)
            if gpu is None:
                fail(f"VRAM insuffisante pour {STT_BACKEND}")
                RESULTS["stt_asr"] = False
            else:
                ok(f"VRAM: GPU {gpu} alloué ({vram_needed} Mo requis pour {STT_BACKEND})")

                transcriber = create_transcriber(cfg, backend=STT_BACKEND, device=f"cuda:{gpu}")
                if not transcriber.available:
                    warn(f"{STT_BACKEND} non disponible, chargement...")
                    loaded = transcriber.load()
                    ok(f"{STT_BACKEND} chargé : {loaded}")

                print(f"    ⏳ Transcription {STT_BACKEND} en cours...")
                segments = transcriber.transcribe(audio_path, language="fr", chunk_length_s=30)
                transcriber.offload()

                gpu_after_stt = gpu_checkpoint(f"APRÈS {STT_BACKEND} ASR (offload)")
                verify_gpu_activity(gpu_before_stt, gpu_after_stt, f"{STT_BACKEND} ASR", expected_min_vram_mb=500)

                transcript_text = "\n".join(
                    f"[{s.get('start', 0):.1f}s → {s.get('end', 0):.1f}s] {s.get('speaker', '')} {s.get('text', s.get('error', ''))}"
                    for s in segments
                )
                fs.save_text("summary/quick_transcript.txt", transcript_text)
                fs.save_json("summary/summary.json", {"segments": segments})

                transcript_short = "\n".join(s.get("text", s.get("error", "")) for s in segments[:50])
                ok(f"Segments : {len(segments)} | Durée : {segments[-1].get('end', 0):.1f}s")
                ok(f"Texte : {transcript_short[:120]}...")

                # ── 4b. Résumé LLM (Qwen) — GPU CHECKPOINT ──
                llm_config = cfg.get("workflow", {}).get("summary_llm", {})
                if llm_config.get("enabled") and not SKIP_LLM:
                    step("Résumé LLM via Qwen 35B")
                    timer_start("llm_summary")
                    vram.offload_all()
                    gpu_before_llm = gpu_checkpoint("AVANT Qwen 35B (résumé)")

                    launched = vram.launch_qwen_35b()
                    if launched:
                        ok("Qwen 35B lancé sur le port 8080")
                        print("    ⏳ Attente que Qwen soit prêt...")
                        # wait for model to be ready (launch_qwen_35b already waits)
                        gpu_after_llm_launch = gpu_checkpoint("Qwen 35B chargé (résumé)")

                        from transcria.gpu.opencode_runner import OpenCodeRunner
                        oc_runner = OpenCodeRunner(str(fs.job_dir / "summary"))
                        print("    ⏳ Génération du résumé...")
                        summary_result = oc_runner.run_summary(
                            str(fs.job_dir / "summary" / "quick_transcript.txt"),
                            str(fs.job_dir / "context" / "job_context.yaml") if (fs.job_dir / "context" / "job_context.yaml").exists() else None,
                        )

                        vram.stop_qwen_35b()
                        gpu_after_llm = gpu_checkpoint("APRÈS Qwen 35B (résumé, arrêt)")
                        verify_gpu_activity(gpu_before_llm, gpu_after_llm_launch, "Qwen 35B résumé", expected_min_vram_mb=1000)

                        if summary_result.get("summary_text") and "indisponible" not in summary_result["summary_text"].lower():
                            ok(f"Résumé LLM : {len(summary_result['summary_text'])} caractères")
                            # Save LLM summary
                            summary_text = summary_result.get("summary_text", "")
                            if summary_text:
                                fs.save_text("summary/summary.md",
                                    f"# Résumé de contrôle\n\n{summary_text}\n\n---\n\n## Extrait de transcription\n\n{transcript_short}\n")
                                meeting_ctx = fs.load_json("context/meeting_context.json") or {}
                                for field in ["title_suggere", "type_suggere", "sujet_suggere", "objectif_suggere",
                                               "notes_suggeres", "participants_detectes", "mots_cles"]:
                                    if summary_result.get(field):
                                        meeting_ctx[field] = summary_result[field]
                                if summary_result.get("speaker_count", 0) > 0:
                                    meeting_ctx["speaker_count_llm"] = summary_result["speaker_count"]
                                meeting_ctx["summary_text"] = summary_text
                                fs.save_json("context/meeting_context.json", meeting_ctx)
                        else:
                            warn("Résumé LLM indisponible ou vide")

                    else:
                        warn("Qwen 35B non disponible — résumé rapide uniquement")
                    timer_end("llm_summary")
                else:
                    if SKIP_LLM:
                        warn("LLM ignoré (--skip-llm)")
                    else:
                        warn("LLM désactivé dans la config — résumé rapide uniquement")
                    # Use quick summary only
                    summary_text = f"# Résumé de contrôle\n\nRésumé rapide indisponible.\n\n---\n\n## Extrait\n\n{transcript_short}\n"
                    fs.save_text("summary/summary.md", summary_text)

                JobStore.update_state(job.id, JS.SUMMARY_DONE)
                RESULTS["stt_asr"] = True
        except Exception as e:
            fail("Échec transcription", str(e))
            RESULTS["stt_asr"] = False
            traceback.print_exc()
        timer_end("stt_asr")

        # ─────────────────────────────────────────────────────────────
        #  5. PYANNOTE DIARIZATION — GPU CHECKPOINT
        # ─────────────────────────────────────────────────────────────
        if not SKIP_DIAR:
            step("Diarisation pyannote")
            timer_start("pyannote")
            gpu_before_diar = gpu_checkpoint("AVANT pyannote")

            try:
                diarizer = DiarizerService(cfg, device="cuda:0")
                if diarizer.available:
                    runner_wr = WorkflowRunner(JobStore, cfg)
                    print("    ⏳ Diarisation en cours...")
                    speaker_result = runner_wr.run_speaker_detection(job, str(audio_path), cfg)

                    gpu_after_diar = gpu_checkpoint("APRÈS pyannote (offload)")
                    verify_gpu_activity(gpu_before_diar, gpu_after_diar, "pyannote", expected_min_vram_mb=500)

                    if speaker_result.get("available"):
                        ok(f"Locuteurs : {len(speaker_result.get('speakers', []))}")
                        for spk in speaker_result.get("speakers", []):
                            ok(f"  {spk.get('speaker_id', '?')}: {spk.get('speaking_time_seconds', 0):.1f}s, {spk.get('turn_count', 0)} tours")
                    else:
                        warn(f"Diarisation indisponible : {speaker_result.get('message', '?')}")
                    RESULTS["pyannote"] = True
                else:
                    warn("pyannote non disponible — étape ignorée")
                    RESULTS["pyannote"] = None
            except Exception as e:
                fail("Échec diarisation", str(e))
                RESULTS["pyannote"] = False
                traceback.print_exc()
            timer_end("pyannote")
        else:
            warn("Diarisation ignorée (--skip-diarization)")
            RESULTS["pyannote"] = None

        # ── 6. Contexte ──
        step("Contexte de réunion")
        timer_start("context")
        MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], {
            "title": TEST_JOB_TITLE, "language": "fr", "meeting_type": "Formation",
            "sensitivity": "normal", "objective": "Test E2E avec contrôle GPU",
        })
        JobStore.update_state(job.id, JS.CONTEXT_DONE)
        ok("Contexte sauvegardé")
        RESULTS["context"] = True
        timer_end("context")

        # ── 7. Participants ──
        step("Participants")
        timer_start("participants")
        participants = [
            {"name": "Alice Martin", "function": "DSI", "is_animator": True},
            {"name": "Bob Dupont", "function": "DCF", "is_animator": False},
        ]
        ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], participants)
        JobStore.update_state(job.id, JS.PARTICIPANTS_DONE)
        ok(f"Participants : {len(participants)}")
        RESULTS["participants"] = True
        timer_end("participants")

        # ── 8. Lexique ──
        step("Lexique métier")
        timer_start("lexicon")
        lexicon = [
            {"term": "API Gateway", "category": "technique", "priority": "critique"},
            {"term": "Kubernetes", "category": "technique", "priority": "normale"},
            {"term": "CI/CD", "category": "technique", "priority": "normale"},
        ]
        LexiconManager.save(job, cfg["storage"]["jobs_dir"], lexicon)
        JobStore.update_state(job.id, JS.LEXICON_DONE)
        JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])
        ok(f"Lexique : {len(lexicon)} termes")
        RESULTS["lexicon"] = True
        timer_end("lexicon")

        # ── 9. Mapping ──
        step("Mapping locuteurs")
        timer_start("mapping")
        speaker_stats = fs.load_json("speakers/speaker_stats.json")
        if speaker_stats and speaker_stats.get("speakers"):
            mapping = {}
            participants_saved = ParticipantsManager.get(job, cfg["storage"]["jobs_dir"])
            for i, spk in enumerate(speaker_stats["speakers"]):
                spk_id = spk.get("speaker_id", spk.get("label", f"SPEAKER_0{i}"))
                if i < len(participants_saved):
                    mapping[spk_id] = {"name": participants_saved[i].get("name", spk_id), "participant_id": participants_saved[i].get("id", "")}
                else:
                    mapping[spk_id] = {"name": spk_id, "participant_id": ""}
            SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
            JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])
            ok(f"Mapping : {len(mapping)} locuteurs")
        else:
            warn("Aucun locuteur — mapping ignoré")
        JobStore.update_state(job.id, JS.READY_TO_PROCESS)
        RESULTS["mapping"] = True
        timer_end("mapping")

        # ─────────────────────────────────────────────────────────────
        #  10. TRAITEMENT — Cohere + qualité + export
        # ─────────────────────────────────────────────────────────────
        step("Traitement : transcription qualité export")
        timer_start("process")
        gpu_before_process = gpu_checkpoint("AVANT traitement (Cohere #2)")

        try:
            runner = WorkflowRunner(JobStore, cfg)
            job = JobStore.get_by_id(job.id)

            # Cohere transcription (second pass for processing step)
            transcribe_result = runner.run_transcription(job, str(audio_path), cfg)
            if transcribe_result.get("error"):
                fail(f"Transcription : {transcribe_result['error']}")
                RESULTS["process"] = False
            else:
                ok(f"Transcription OK : {transcribe_result.get('speaker_count', '?')} locuteurs")

                gpu_after_transcribe = gpu_checkpoint("APRÈS Cohere #2 (offload)")
                verify_gpu_activity(gpu_before_process, gpu_after_transcribe, "Cohere #2", expected_min_vram_mb=500)

                # Quality
                quality_result = runner.run_quality_checks(job, cfg)
                ok(f"Qualité : {quality_result.get('quality_score', '?')}/100")

                # Export
                export_result = runner.build_export(job, cfg)
                if export_result.get("zip_path"):
                    zip_path = Path(export_result["zip_path"])
                    ok(f"ZIP : {zip_path.name} ({zip_path.stat().st_size / 1024:.1f} Ko)")
                else:
                    warn(f"Export : {export_result}")

                JobStore.update_state(job.id, JS.COMPLETED)
                RESULTS["process"] = True
        except Exception as e:
            fail("Échec traitement", str(e))
            RESULTS["process"] = False
            traceback.print_exc()
        timer_end("process")

        # ─────────────────────────────────────────────────────────────
        #  11. CORRECTION SRT via Qwen 35B — GPU CHECKPOINT
        # ─────────────────────────────────────────────────────────────
        if not SKIP_LLM:
            step("Correction SRT via Qwen 35B")
            timer_start("llm_correction")
            gpu_before_correction = gpu_checkpoint("AVANT Qwen 35B (correction)")

            try:
                runner_cor = WorkflowRunner(JobStore, cfg)
                job = JobStore.get_by_id(job.id)

                correction_result = runner_cor.run_correction(job, cfg)
                if correction_result.get("success") and correction_result.get("corrected_srt"):
                    ok(f"Correction SRT : {len(correction_result['corrected_srt'])} caractères")
                    if correction_result.get("report"):
                        ok(f"Rapport : {len(correction_result['report'])} caractères")
                else:
                    warn(f"Correction SRT : {correction_result.get('error', 'pas de résultat')}")

                gpu_after_correction = gpu_checkpoint("APRÈS Qwen 35B (correction, arrêt)")
                verify_gpu_activity(gpu_before_correction, gpu_after_correction, "Qwen 35B correction", expected_min_vram_mb=1000)
                RESULTS["llm_correction"] = True
            except Exception as e:
                fail("Échec correction", str(e))
                RESULTS["llm_correction"] = False
                traceback.print_exc()
            timer_end("llm_correction")
        else:
            warn("Correction LLM ignorée (--skip-llm)")
            RESULTS["llm_correction"] = None

        # ── Vérification fichiers ──
        step("Vérification des fichiers produits")
        timer_start("verify")
        job = JobStore.get_by_id(job.id)
        ok(f"État final : {job.state}")

        expected_files = [
            "input/original.mp3", "metadata/audio_analysis.json",
            "summary/quick_transcript.txt", "summary/summary.json", "summary/summary.md",
            "context/meeting_context.json", "context/participants.json",
            "context/session_lexicon.json", "context/job_context.yaml",
        ]
        found = missing = 0
        for f in expected_files:
            if (fs.job_dir / f).exists():
                found += 1
            else:
                missing += 1
                warn(f"  ✗ {f}")
        ok(f"Fichiers : {found}/{len(expected_files)} trouvés")

        # Snapshot final
        gpu_final = gpu_checkpoint("État final GPU")
        RESULTS["verify"] = missing == 0
        timer_end("verify")

        # ── TESTS NOUVEAUX COMPOSANTS ──
        if TEST_NEW_COMPONENTS:
            _test_gpu_session(cfg)
            _test_llm_backend(cfg)
            _test_transcriber_factory(cfg, fs)
            _test_summary_via_runner(job, str(audio_path), cfg)

        if TEST_SERVICES:
            _test_job_service(cfg)
            _test_pipeline_service(job, str(audio_path), cfg)

        # ── Résumé ──
        section("Détails du contenu du job")
        for subdir in ["input", "metadata", "summary", "context", "speakers", "quality", "exports"]:
            dir_path = fs.job_dir / subdir
            if dir_path.exists():
                files = list(dir_path.iterdir())
                print(f"  {subdir}/ : {len(files)} fichier(s)")
                for f in sorted(files):
                    if f.is_file():
                        print(f"    {f.name} ({f.stat().st_size} octets)")

        if not KEEP_JOB:
            section("Nettoyage")
            import shutil
            print(f"  Suppression du job : {fs.job_dir}")
            shutil.rmtree(fs.job_dir, ignore_errors=True)
        else:
            section("Job conservé")
            print(f"  Job ID : {job.id}")
            print(f"  Répertoire : {fs.job_dir}")

    print_summary()


if __name__ == "__main__":
    main()
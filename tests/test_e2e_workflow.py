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
5. PipelineService.run_process(..., mode=<fast|quality>) :
   - analyse de scène audio (subprocess librosa, pré-transcription)
   - séparation de sources optionnelle (Demucs, pré-transcription)
   - filtrage scène optionnel
   - normalisation audio optionnelle
   - transcription finale (Cohere ou Whisper)
   - diarisation pyannote (mode quality uniquement)
   - correction LLM d'arbitrage
   - contrôle qualité
   - export ZIP

Prérequis : arrêter le service TranscrIA avant d'exécuter ce test.

    systemctl stop transcria
    venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3
    systemctl start transcria

Utilisation bench (plusieurs combos en parallèle) :
    venv/bin/python tests/test_e2e_workflow.py \\
        --audio tests/test1.mp3 \\
        --gpu 3 \\
        --stt-backend whisper \\
        --enable-audio-scene \\
        --force-source-separation \\
        --enable-audio-normalization \\
        --combo-id 023 \\
        --output-json /tmp/bench/023.json \\
        --skip-llm --keep

Topologie frontale + ressources distantes (serveurs lancés à part) :
    # STT distant (scripts/launch_stt_cohere.sh sur le GPU 3) ; pipeline sur GPU 0
    venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 \\
        --gpu 0 --stt-backend cohere \\
        --remote-stt http://192.168.1.59:8003/v1 --skip-llm
    # + diarisation/voice-embed distantes (inference_service sur :8002)
    venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 \\
        --gpu 0 --mode quality \\
        --remote-stt http://192.168.1.59:8005/v1 --stt-backend whisper \\
        --remote-inference http://192.168.1.59:8002 --skip-llm
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Mise en place GPU AVANT tout import CUDA/torch/pyannote/faster-whisper
# CUDA_VISIBLE_DEVICES doit être positionné avant que ces bibliothèques
# ne lisent la liste des devices disponibles.
# ─────────────────────────────────────────────────────────────────────────────
def _early_gpu_setup() -> str | None:
    """Parse --gpu depuis sys.argv et positionne TRANSCRIA_PREFERRED_GPU.

    CUDA_VISIBLE_DEVICES n'est PAS utilisé car le VRAMManager scanne nvidia-smi
    (qui ignore CUDA_VISIBLE_DEVICES) et passe des indices physiques à PyTorch.
    Combiner les deux cause des "invalid device ordinal" au chargement des modèles.
    TRANSCRIA_PREFERRED_GPU est lu par VRAMManager comme GPU de départ préféré
    avant le fallback sur le meilleur GPU libre, ce qui évite les collisions VRAM
    quand plusieurs workers tournent en parallèle sur des GPUs différents.
    """
    for i, arg in enumerate(sys.argv):
        if arg == "--gpu" and i + 1 < len(sys.argv):
            gpu_val = sys.argv[i + 1]
            # Prendre le premier GPU de la liste si plusieurs sont spécifiés
            primary_gpu = gpu_val.split(",")[0]
            os.environ["TRANSCRIA_PREFERRED_GPU"] = primary_gpu
            print(f"[early-gpu] --gpu={gpu_val!r} → TRANSCRIA_PREFERRED_GPU={primary_gpu}",
                  flush=True)
            return gpu_val
    return None

_EARLY_GPU = _early_gpu_setup()

# ─────────────────────────────────────────────────────────────────────────────
# Imports projet (après la mise en place GPU)
# ─────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TRANSCRIA_CONFIG", str(Path(__file__).parent.parent / "config.yaml"))

from app import create_app
from transcria.config import load_config, set_config
from transcria.database import db

# ─────────────────────────────────────────────────────────────────────────────
# Constantes et état global
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_AUDIO = Path(__file__).parent / "test1.mp3"
DEFAULT_JOB_TITLE = "E2E workflow production"

logger = logging.getLogger("e2e")

STEP = 0
RESULTS: dict[str, bool | None | str] = {}
TIMINGS: dict[str, float] = {}
GPU_SNAPSHOTS: list[dict] = []
ERRORS: list[str] = []
RUN_STARTED_AT: str = datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test E2E TranscrIA proche production",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  # Run basique
  venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3 --keep

  # Run sans LLM, Whisper, tout le prétraitement audio
  venv/bin/python tests/test_e2e_workflow.py \\
      --audio tests/test2.mp3 --skip-llm \\
      --stt-backend whisper --whisper-model-size large-v3 \\
      --enable-audio-scene --enable-scene-filter \\
      --enable-audio-normalization --force-source-separation --keep

  # Run benchmark (appelé par bench_audio.py)
  venv/bin/python tests/test_e2e_workflow.py \\
      --audio tests/test1.mp3 --gpu 3 --skip-llm \\
      --stt-backend cohere --combo-id 001 \\
      --output-json /tmp/bench/001.json --keep
""",
    )

    # ── Audio ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--audio", type=Path, default=DEFAULT_AUDIO,
        help="Fichier audio à utiliser (défaut: tests/test1.mp3)",
    )
    parser.add_argument(
        "--job-title", type=str, default=DEFAULT_JOB_TITLE,
        help="Titre du job créé (utile pour l'identification en bench)",
    )

    # ── STT ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--stt-backend", choices=["cohere", "cohere_tf5", "whisper", "granite", "parakeet"], default="cohere",
        help=(
            "Backend STT demandé au départ (défaut: cohere). "
            "Le backend effectif est tracé dans metadata/transcription_metadata.json."
        ),
    )
    parser.add_argument(
        "--whisper-model-size",
        choices=["tiny", "base", "small", "medium", "large-v1", "large-v2",
                 "large-v3", "distil-large-v2", "turbo"],
        default="large-v3",
        help="Taille du modèle Whisper si --stt-backend=whisper (défaut: large-v3)",
    )
    parser.add_argument(
        "--remote-stt", metavar="URL", default=None,
        help=(
            "Servir le STT à distance via une API compatible OpenAI (vLLM/SGLang), "
            "ex: http://192.168.1.59:8003/v1. Force inference.mode=remote pour le "
            "backend --stt-backend, response_format auto (cohere=json, whisper="
            "verbose_json) et fallback_local=False (échec bruyant si le serveur tombe)."
        ),
    )
    parser.add_argument(
        "--remote-stt-api-key", metavar="KEY", default=None,
        help="Clé API du serveur STT distant (si lancé avec --api-key).",
    )
    parser.add_argument(
        "--remote-inference", metavar="URL", default=None,
        help=(
            "Servir diarisation + empreinte vocale à distance via le service Flask "
            "inference_service, ex: http://192.168.1.59:8002. Force diarization_backend"
            "=remote, inference.mode=remote, transport.audio=upload (requis en distant) "
            "et fallback_local=False. Effet sur la diarisation en mode quality."
        ),
    )
    parser.add_argument(
        "--remote-inference-api-key", metavar="KEY", default=None,
        help="Clé API du service d'inférence distant (Authorization: Bearer).",
    )

    # ── Mode pipeline ────────────────────────────────────────────────────────
    parser.add_argument(
        "--mode", choices=["fast", "quality"], default="quality",
        help="Mode du pipeline final — quality active la diarisation (défaut: quality)",
    )

    # ── Désactivations ───────────────────────────────────────────────────────
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Désactiver résumé LLM et correction LLM (STT et diarisation conservés)",
    )
    parser.add_argument(
        "--skip-diarization", action="store_true",
        help="Désactiver pyannote (accélère le run, pas de locuteurs)",
    )
    parser.add_argument(
        "--skip-summary", action="store_true",
        help="Passer la phase résumé (WorkflowRunner.run_summary) — pipeline uniquement",
    )

    # ── Options prétraitement audio ──────────────────────────────────────────
    parser.add_argument(
        "--enable-audio-scene", action="store_true",
        help="Forcer workflow.audio_scene.enabled=true",
    )
    parser.add_argument(
        "--enable-scene-filter", action="store_true",
        help="Forcer le filtrage scène pré-STT (implique --enable-audio-scene)",
    )
    parser.add_argument(
        "--enable-audio-normalization", action="store_true",
        help="Forcer la normalisation audio pré-STT",
    )
    parser.add_argument(
        "--disable-audio-preflight", action="store_true",
        help="Désactiver workflow.audio_preflight.enabled (baseline sans pré-diagnostic)",
    )
    parser.add_argument(
        "--disable-weak-voice-normalization", action="store_true",
        help="Désactiver workflow.audio_normalization.weak_voice.enabled",
    )
    parser.add_argument(
        "--enable-audio-denoise", action="store_true",
        help="Activer le débruitage expérimental workflow.audio_denoise.enabled",
    )
    parser.add_argument(
        "--force-audio-denoise", action="store_true",
        help="Forcer le débruitage expérimental quel que soit audio_preflight "
             "(implique --enable-audio-denoise)",
    )
    parser.add_argument(
        "--disable-segment-reliability", action="store_true",
        help="Désactiver workflow.segment_reliability.enabled",
    )
    parser.add_argument(
        "--disable-micro-chunk-merge", action="store_true",
        help="Désactiver workflow.pyannote_chunking.merge_micro_chunks",
    )
    parser.add_argument(
        "--enable-vad-hysteresis", action="store_true",
        help="Activer workflow.vad.hysteresis_enabled avec onset/offset configurés",
    )
    parser.add_argument(
        "--enable-source-separation", action="store_true",
        help="Activer le service Demucs (décision soumise aux seuils internes)",
    )
    parser.add_argument(
        "--force-source-separation", action="store_true",
        help="Forcer Demucs quel que soit le résultat de l'analyse de scène "
             "(bypasse les seuils — implique --enable-source-separation)",
    )
    parser.add_argument(
        "--config-override", action="append", default=[],
        metavar="CLE=VALEUR",
        help="Override YAML ponctuel, ex: workflow.vad.enabled_final=true "
             "ou whisper.no_speech_threshold=0.4. Répétable.",
    )
    parser.add_argument(
        "--enable-whisper-lexicon-hotwords", action="store_true",
        help="Activer whisper.lexicon_hotwords.enabled pour ce run.",
    )
    parser.add_argument(
        "--enable-cohere-lexicon-biasing", action="store_true",
        help="Activer cohere.lexicon_biasing.enabled pour ce run expérimental.",
    )
    parser.add_argument(
        "--lexicon-term", action="append", default=[],
        metavar="TERME[|priorité|catégorie|variante1;variante2]",
        help="Ajoute un terme au lexique de session E2E. Répétable.",
    )
    parser.add_argument(
        "--lexicon-json", type=Path, default=None,
        help="Fichier JSON contenant une liste d'entrées de lexique à ajouter au run.",
    )

    # ── GPU et LLM ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--gpu", type=str, default=None,
        help="GPU(s) à utiliser : '3' ou '3,4' → CUDA_VISIBLE_DEVICES "
             "(doit correspondre à l'arg passé au démarrage pour le early-setup)",
    )
    parser.add_argument(
        "--arbitrage-port", type=int, default=None,
        help="Port de la LLM d'arbitrage (défaut: valeur config.yaml, souvent 8080) — "
             "utile pour les runs parallèles avec plusieurs instances LLM",
    )
    parser.add_argument(
        "--schedule-case",
        choices=["none", "pause_queue", "pause_then_release", "limit_concurrency", "force_gpu"],
        default="none",
        help=(
            "Injecte une fenêtre de planification active et vérifie son effet avant le pipeline. "
            "pause_queue, pause_then_release et limit_concurrency utilisent une entrée job_queue de test ; "
            "force_gpu vérifie seulement l'autorisation de fenêtre, sans tuer de processus GPU."
        ),
    )
    parser.add_argument(
        "--schedule-limit-workers",
        type=int,
        default=1,
        help="Limite max_concurrent_jobs appliquée au cas --schedule-case limit_concurrency.",
    )
    parser.add_argument(
        "--process-via-api",
        action="store_true",
        help=(
            "Lance le traitement final via POST /api/jobs/<id>/process avec la file activée, "
            "puis attend la fin du scheduler au lieu d'appeler PipelineService directement."
        ),
    )
    parser.add_argument(
        "--queue-api-timeout-s",
        type=int,
        default=900,
        help="Timeout du polling quand --process-via-api est actif.",
    )

    # ── Bench ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--combo-id", type=str, default=None,
        help="Identifiant de la combinaison (ex: '001') — reporté dans --output-json "
             "pour l'intégration avec bench_audio.py",
    )
    parser.add_argument(
        "--output-json", type=Path, default=None,
        help="Chemin du fichier JSON de résultats structurés (pour bench_audio.py). "
             "Créé à la fin du run.",
    )

    # ── Gestion du job ───────────────────────────────────────────────────────
    parser.add_argument(
        "--keep", action="store_true",
        help="Conserver le job à la fin du run (pour inspection manuelle des SRTs)",
    )
    parser.add_argument(
        "--keep-on-error", action="store_true",
        help="Conserver le job même si le pipeline échoue (facilite le débogage)",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Affichage structuré
# ─────────────────────────────────────────────────────────────────────────────
def _ts() -> str:
    """Horodatage court pour les logs console."""
    return datetime.now().strftime("%H:%M:%S")


def step(name: str) -> None:
    global STEP
    STEP += 1
    print(f"\n{'=' * 72}")
    print(f"  [{_ts()}] ETAPE {STEP} : {name}")
    print(f"{'=' * 72}")


def section(title: str) -> None:
    print(f"\n{'-' * 72}")
    print(f"  [{_ts()}] {title}")
    print(f"{'-' * 72}")


def ok(message: str) -> None:
    print(f"  [{_ts()}] OK   {message}")


def warn(message: str) -> None:
    print(f"  [{_ts()}] WARN {message}")


def fail(message: str, error: object | None = None) -> None:
    msg = f"  [{_ts()}] FAIL {message}"
    if error:
        msg += f"\n         {error}"
    print(msg)
    ERRORS.append(f"{message}: {error}" if error else message)


def info(message: str) -> None:
    print(f"  [{_ts()}]      {message}")


def _parse_override_value(raw: str) -> object:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return ast.literal_eval(raw)
    except Exception:
        return raw


def _set_nested_config(cfg: dict, dotted_key: str, value: object) -> None:
    parts = [part.strip() for part in dotted_key.split(".") if part.strip()]
    if not parts:
        raise ValueError("clé vide")
    node = cfg
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"{'.'.join(parts[:-1])}: chemin non objet")
        node = child
    node[parts[-1]] = value


def apply_config_overrides(cfg: dict, overrides: list[str]) -> dict:
    applied: dict[str, object] = {}
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"Override invalide {item!r}: attendu CLE=VALEUR")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        value = _parse_override_value(raw_value.strip())
        _set_nested_config(cfg, key, value)
        applied[key] = value
        info(f"Override config: {key}={value!r}")
    return applied


# ─────────────────────────────────────────────────────────────────────────────
# Timers
# ─────────────────────────────────────────────────────────────────────────────
def timer_start(name: str) -> None:
    TIMINGS[name] = time.time()


def timer_end(name: str) -> float:
    elapsed = time.time() - TIMINGS[name]
    print(f"  [{_ts()}]      Durée {name}: {elapsed:.1f}s")
    TIMINGS[name] = elapsed
    return elapsed


# ─────────────────────────────────────────────────────────────────────────────
# GPU monitoring
# ─────────────────────────────────────────────────────────────────────────────
def get_gpu_snapshot(label: str) -> dict:
    snapshot: dict = {
        "label": label,
        "timestamp": time.time(),
        "gpus": [],
        "processes": [],
    }
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.free,memory.total,"
                "utilization.gpu,utilization.memory,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                snapshot["gpus"].append({
                    "id": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mb": int(parts[2]),
                    "mem_free_mb": int(parts[3]),
                    "mem_total_mb": int(parts[4]),
                    "gpu_util_pct": int(parts[5]),
                    "mem_util_pct": int(parts[6]),
                    "temp_c": int(parts[7]) if len(parts) > 7 else None,
                })
    except Exception as exc:
        snapshot["gpu_error"] = str(exc)

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                try:
                    snapshot["processes"].append({
                        "pid": int(parts[0]),
                        "name": parts[1],
                        "vram_mb": int(parts[2]),
                    })
                except ValueError:
                    pass
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
        temp = f", {gpu['temp_c']}°C" if gpu.get("temp_c") is not None else ""
        print(
            f"{prefix}GPU {gpu['id']} ({gpu['name']}): "
            f"{gpu['mem_used_mb']:>6}/{gpu['mem_total_mb']} Mo "
            f"(libre {gpu['mem_free_mb']:>6} Mo)"
            f", util {gpu['gpu_util_pct']:>3}%{temp}"
        )
    processes = snapshot.get("processes") or []
    if processes:
        for proc in processes:
            print(f"{prefix}  PID {proc['pid']:>7}: {proc['name']} ({proc['vram_mb']} Mo)")
    else:
        print(f"{prefix}  (aucun processus GPU actif)")


def gpu_checkpoint(label: str) -> dict:
    snapshot = get_gpu_snapshot(label)
    print_gpu_state(snapshot)
    return snapshot


class _VRAMMonitor:
    """Thread de surveillance VRAM en arrière-plan.

    Échantillonne nvidia-smi toutes les interval_s secondes et stocke
    les snapshots dans GPU_SNAPSHOTS pour un calcul de peak VRAM fiable.
    """

    def __init__(self, interval_s: float = 3.0):
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counter = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="vram-monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set():
                break
            self._counter += 1
            get_gpu_snapshot(f"vram-monitor-{self._counter:03d}")


def _peak_vram_mb(snapshots: list[dict], gpu_id: str | None = None) -> int | None:
    """VRAM max consommée sur l'ensemble des snapshots.

    Si gpu_id est fourni, retourne le peak sur ce GPU uniquement.
    Sinon, retourne le peak global (tous GPUs confondus).
    """
    target_ids = None
    if gpu_id is not None:
        target_ids = {str(gpu_id), str(int(gpu_id))} if gpu_id.isdigit() else {str(gpu_id)}
    peak = None
    for snap in snapshots:
        gpus = snap.get("gpus") or []
        for g in gpus:
            if target_ids is not None and str(g["id"]) not in target_ids:
                continue
            used = g["mem_used_mb"]
            if peak is None or used > peak:
                peak = used
    return peak


# ─────────────────────────────────────────────────────────────────────────────
# Configuration et feature flags
# ─────────────────────────────────────────────────────────────────────────────
def apply_e2e_config(cfg: dict, args: argparse.Namespace) -> None:
    """Applique tous les overrides de config demandés par les arguments.

    En mode bench (--combo-id présent), toutes les options audio non demandées
    sont explicitement forcées à OFF pour neutraliser les valeurs du config.yaml
    de production (ex: audio_scene.enabled: true).
    """
    bench_mode = args.combo_id is not None

    # STT backend et taille Whisper
    cfg.setdefault("models", {})["stt_backend"] = args.stt_backend
    if args.stt_backend == "whisper":
        cfg.setdefault("whisper", {})["model_size"] = args.whisper_model_size
        info(f"Whisper model_size forcé à {args.whisper_model_size!r}")

    # STT distant : le pipeline transcrit via un serveur compatible OpenAI.
    if args.remote_stt:
        # Cohere Transcribe (vLLM) refuse verbose_json → texte ; Whisper gère les segments.
        rfmt = "json" if args.stt_backend == "cohere" else "verbose_json"
        inference = cfg.setdefault("inference", {})
        inference["mode"] = "remote"
        stt = inference.setdefault("stt", {})
        stt["fallback_local"] = False  # banc : échec bruyant, on teste vraiment le distant
        if args.remote_stt_api_key:
            stt.setdefault("auth", {})["api_key"] = args.remote_stt_api_key
        backends = stt.setdefault("backends", {})
        be = backends.setdefault(args.stt_backend, {})
        be["url"] = args.remote_stt
        be["response_format"] = rfmt
        # served-model-name attendu par le serveur (cf. launch_stt_*.sh) ; ne pas
        # écraser une valeur déjà fournie par la config.
        be.setdefault("model", {"cohere": "cohere-transcribe",
                                "whisper": "whisper-large-v3"}.get(args.stt_backend, args.stt_backend))
        info(f"STT distant : {args.stt_backend} → {args.remote_stt} "
             f"(model={be['model']}, response_format={rfmt}, fallback_local=off)")

    # Diarisation + empreinte vocale distantes via le service Flask inference_service.
    if args.remote_inference:
        inference = cfg.setdefault("inference", {})
        inference["mode"] = "remote"            # active la sélection voice-embed distante
        inference["url"] = args.remote_inference
        inference["fallback_local"] = False     # banc : échec bruyant
        # upload OBLIGATOIRE en distant : file_ref enverrait un chemin que le
        # service ne peut pas résoudre (filesystem non partagé).
        inference.setdefault("transport", {})["audio"] = "upload"
        if args.remote_inference_api_key:
            inference.setdefault("auth", {})["api_key"] = args.remote_inference_api_key
        # RemoteDiarizer est choisi par le NOM de backend (pas par inference.mode).
        cfg.setdefault("models", {})["diarization_backend"] = "remote"
        info(f"Inférence distante (diarize+voice-embed) : {args.remote_inference} "
             f"(transport=upload, fallback_local=off, diarization_backend=remote)")

    # LLM arbitrage port
    if args.arbitrage_port is not None:
        cfg.setdefault("services", {})["arbitrage_llm_port"] = args.arbitrage_port
        info(f"Port LLM arbitrage forcé à {args.arbitrage_port}")

    # Désactivations LLM
    if args.skip_llm:
        cfg.setdefault("workflow", {}).setdefault("summary_llm", {})["enabled"] = False
        cfg.setdefault("workflow", {}).setdefault("arbitration_llm", {})["enabled"] = False
        info("LLM résumé et correction désactivées (--skip-llm)")

    # Désactivation diarisation
    if args.skip_diarization:
        cfg.setdefault("workflow", {})["enable_speaker_detection"] = False
        info("Diarisation pyannote désactivée (--skip-diarization)")

    # ── Prétraitement audio ───────────────────────────────────────────────────
    # En mode bench : état explicite pour chaque option (ON ou OFF).
    # Hors bench : uniquement les flags --enable-* activent des options ;
    #              les options absentes gardent leur valeur config.yaml.

    want_scene = args.enable_audio_scene or args.enable_scene_filter
    want_filter = args.enable_scene_filter
    want_norm = args.enable_audio_normalization
    want_sep = args.enable_source_separation or args.force_source_separation
    want_denoise = args.enable_audio_denoise or args.force_audio_denoise

    if bench_mode:
        # Forcer l'état de chaque option — neutralise config.yaml de production
        cfg.setdefault("workflow", {}).setdefault("audio_scene", {})["enabled"] = want_scene
        cfg["workflow"].setdefault("audio_scene_filter", {})["enabled"] = want_filter
        cfg["workflow"].setdefault("audio_normalization", {})["enabled"] = want_norm
        cfg["workflow"].setdefault("source_separation", {})["enabled"] = want_sep
        cfg["workflow"]["source_separation"]["force"] = args.force_source_separation
        cfg["workflow"].setdefault("audio_denoise", {})["enabled"] = want_denoise
        cfg["workflow"]["audio_denoise"]["force"] = args.force_audio_denoise
        info(
            f"[bench] options audio fixées — scene={want_scene} sep={want_sep} "
            f"norm={want_norm} filter={want_filter} denoise={want_denoise} "
            f"sep.force={args.force_source_separation} denoise.force={args.force_audio_denoise}"
        )
    else:
        # Mode manuel : activer seulement les options demandées
        if want_scene:
            cfg.setdefault("workflow", {}).setdefault("audio_scene", {})["enabled"] = True
            info("audio_scene activée")
        if want_filter:
            cfg.setdefault("workflow", {}).setdefault("audio_scene_filter", {})["enabled"] = True
            cfg["workflow"]["audio_scene_filter"].setdefault("enabled_for_modes", ["quality"])
            info("audio_scene_filter activé (auto-active audio_scene)")
        if want_norm:
            cfg.setdefault("workflow", {}).setdefault("audio_normalization", {})["enabled"] = True
            cfg["workflow"]["audio_normalization"].setdefault("enabled_for_modes", ["quality"])
            info("audio_normalization activée")
        if want_sep:
            cfg.setdefault("workflow", {}).setdefault("source_separation", {})["enabled"] = True
            info("source_separation activée")
        if args.force_source_separation:
            cfg["workflow"]["source_separation"]["force"] = True
            info("source_separation.force=true — Demucs s'exécutera quel que soit l'audio")
        if want_denoise:
            cfg.setdefault("workflow", {}).setdefault("audio_denoise", {})["enabled"] = True
            cfg["workflow"]["audio_denoise"].setdefault("enabled_for_modes", ["quality"])
            info("audio_denoise activé")
        if args.force_audio_denoise:
            cfg["workflow"]["audio_denoise"]["force"] = True
            info("audio_denoise.force=true — débruitage expérimental forcé")

    # Options nouvelles non destructives / toggles transversaux
    if args.disable_audio_preflight:
        cfg.setdefault("workflow", {}).setdefault("audio_preflight", {})["enabled"] = False
        info("audio_preflight désactivé")
    else:
        cfg.setdefault("workflow", {}).setdefault("audio_preflight", {})["enabled"] = True

    if args.disable_weak_voice_normalization:
        cfg.setdefault("workflow", {}).setdefault("audio_normalization", {}).setdefault("weak_voice", {})["enabled"] = False
        info("profil weak_voice désactivé")

    if args.disable_segment_reliability:
        cfg.setdefault("workflow", {}).setdefault("segment_reliability", {})["enabled"] = False
        info("segment_reliability désactivé")
    else:
        cfg.setdefault("workflow", {}).setdefault("segment_reliability", {})["enabled"] = True

    if args.disable_micro_chunk_merge:
        cfg.setdefault("workflow", {}).setdefault("pyannote_chunking", {})["merge_micro_chunks"] = False
        info("fusion micro-chunks pyannote désactivée")

    if args.enable_vad_hysteresis:
        cfg.setdefault("workflow", {}).setdefault("vad", {})["hysteresis_enabled"] = True
        info("VAD hysteresis activée")

    if args.enable_whisper_lexicon_hotwords:
        cfg.setdefault("whisper", {}).setdefault("lexicon_hotwords", {})["enabled"] = True
        info("Hotwords Whisper depuis lexique activés")

    if args.enable_cohere_lexicon_biasing:
        cfg.setdefault("cohere", {}).setdefault("lexicon_biasing", {})["enabled"] = True
        info("Biasing Cohere depuis lexique activé")

    if args.schedule_case != "none":
        workflow = cfg.setdefault("workflow", {})
        workflow.setdefault("scheduling", {})["enabled"] = True
        workflow.setdefault("scheduling", {})["timezone"] = workflow["scheduling"].get("timezone", "Europe/Paris")
        # Le pipeline E2E s'exécute directement via PipelineService. On désactive
        # le scheduler global Flask pour éviter qu'il consomme la sonde queue avant
        # le contrôle manuel du cas d'agenda.
        workflow.setdefault("queue", {})["enabled"] = False
        info(f"Cas agenda E2E activé : {args.schedule_case}")

    if args.process_via_api:
        workflow = cfg.setdefault("workflow", {})
        workflow.setdefault("queue", {})["enabled"] = True
        workflow["queue"]["poll_interval_s"] = 1
        workflow.setdefault("execution", {})["max_concurrent_jobs"] = 1
        workflow.setdefault("scheduling", {})["enabled"] = False
        info("Traitement final via API + file persistante activé")


def print_effective_config(cfg: dict, args: argparse.Namespace) -> None:
    """Affiche la configuration effective de ce run."""
    section("Configuration effective du run")
    workflow = cfg.get("workflow", {})
    models = cfg.get("models", {})
    whisper = cfg.get("whisper", {})
    services = cfg.get("services", {})

    print(f"  STT backend          : {models.get('stt_backend', 'cohere')}")
    inference = cfg.get("inference", {})
    if inference.get("mode") in ("remote", "hybrid"):
        be = (inference.get("stt", {}).get("backends", {}) or {}).get(models.get("stt_backend"), {})
        if be.get("url"):
            print(f"  STT distant          : {be['url']} "
                  f"(format={be.get('response_format', '?')}, "
                  f"fallback_local={inference.get('stt', {}).get('fallback_local')})")
    if models.get("diarization_backend") == "remote" or inference.get("url"):
        print(f"  Inférence distante   : {inference.get('url', '?')} "
              f"(transport={inference.get('transport', {}).get('audio', 'file_ref')}, "
              f"diarize_backend={models.get('diarization_backend', 'pyannote')})")
    if models.get("stt_backend") == "whisper":
        print(f"  Whisper model_size   : {whisper.get('model_size', 'large-v3')}")
        print(
            "  Whisper lex hotwords : "
            f"{'OUI' if whisper.get('lexicon_hotwords', {}).get('enabled', False) else 'non'}"
        )
    if models.get("stt_backend") == "cohere":
        print(
            "  Cohere lex biasing   : "
            f"{'OUI' if cfg.get('cohere', {}).get('lexicon_biasing', {}).get('enabled', False) else 'non'}"
        )
    if models.get("stt_backend") == "cohere_tf5":
        cohere_tf5 = cfg.get("cohere_tf5", {})
        print(f"  Cohere TF5 site      : {cohere_tf5.get('tf5_site', '/tmp/transcria_tf54_site')}")
        print(f"  Cohere TF5 batch     : {cohere_tf5.get('batch_size', 96)}")
    if models.get("stt_backend") == "granite":
        granite = cfg.get("granite", {})
        print(f"  Granite model        : {granite.get('model_id', './models/granite-speech-4.1-2b')}")
        print(f"  Granite prompt       : {granite.get('prompt_mode', 'asr_punctuated')}")
    if models.get("stt_backend") == "parakeet":
        parakeet = cfg.get("parakeet", {})
        print(f"  Parakeet model       : {parakeet.get('model_id', 'nvidia/parakeet-tdt-0.6b-v3')}")
        print(f"  Parakeet attention   : {'local' if parakeet.get('use_local_attention', True) else 'full'}")
        print(f"  Parakeet decoding    : {parakeet.get('decoding_strategy', 'greedy_batch')}")
    print(f"  Mode pipeline        : {args.mode}")
    print(f"  LLM résumé           : {'non' if args.skip_llm else 'oui'}")
    print(f"  LLM correction       : {'non' if args.skip_llm else 'oui'}")
    print(f"  Diarisation          : {'non' if args.skip_diarization else 'oui'}")
    print(f"  Phase résumé         : {'non (--skip-summary)' if args.skip_summary else 'oui'}")
    print(f"  Port LLM arbitrage   : {services.get('arbitrage_llm_port', 8080)}")
    print(f"  Cas agenda           : {args.schedule_case}")
    print(f"  Traitement via API   : {'oui' if args.process_via_api else 'non'}")
    print()

    opts = [
        ("audio_scene",       workflow.get("audio_scene", {}).get("enabled", False)),
        ("source_separation", workflow.get("source_separation", {}).get("enabled", False)),
        ("source_sep.force",  workflow.get("source_separation", {}).get("force", False)),
        ("audio_scene_filter",workflow.get("audio_scene_filter", {}).get("enabled", False)),
        ("audio_normalization",workflow.get("audio_normalization", {}).get("enabled", False)),
        ("audio_preflight",   workflow.get("audio_preflight", {}).get("enabled", False)),
        ("weak_voice_norm",   workflow.get("audio_normalization", {}).get("weak_voice", {}).get("enabled", True)),
        ("audio_denoise",     workflow.get("audio_denoise", {}).get("enabled", False)),
        ("audio_denoise.force",workflow.get("audio_denoise", {}).get("force", False)),
        ("segment_reliability", workflow.get("segment_reliability", {}).get("enabled", True)),
        ("micro_chunk_merge", workflow.get("pyannote_chunking", {}).get("merge_micro_chunks", True)),
        ("vad_hysteresis",    workflow.get("vad", {}).get("hysteresis_enabled", False)),
    ]
    for name, val in opts:
        flag = "OUI" if val else "non"
        print(f"  {name:22s}: {flag}")

    if args.combo_id:
        print(f"\n  Combo ID             : {args.combo_id}")
    if args.gpu:
        print(f"  CUDA_VISIBLE_DEVICES : {args.gpu}")
    if args.output_json:
        print(f"  Output JSON          : {args.output_json}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de construction du contexte
# ─────────────────────────────────────────────────────────────────────────────
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
        participants.append({
            "id": f"p{idx}",
            "name": "",
            "function": "",
            "service": "",
            "role": "",
            "is_animator": False,
            "expected": True,
            "comment": f"Créé automatiquement pour {speaker.get('speaker_id', f'SPEAKER_{idx:02d}')}",
        })
    if not participants:
        participants.append({
            "id": "p1",
            "name": "",
            "function": "",
            "service": "",
            "role": "",
            "is_animator": False,
            "expected": True,
            "comment": "Participant à identifier",
        })
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
        lexicon.append({
            "term": term,
            "category": item.get("category", "terme_metier"),
            "priority": item.get("priority", "normale"),
            "variants": item.get("variants", []),
            "replace_by": "",
            "comment": item.get("comment", ""),
            "contexts": item.get("contexts", []),
        })
    return lexicon


def _parse_cli_lexicon_term(raw: str) -> dict | None:
    parts = [part.strip() for part in str(raw or "").split("|")]
    term = parts[0] if parts else ""
    if not term:
        return None
    variants = []
    if len(parts) > 3 and parts[3]:
        variants = [variant.strip() for variant in parts[3].split(";") if variant.strip()]
    return {
        "term": term,
        "category": parts[2] if len(parts) > 2 and parts[2] else "terme_metier",
        "priority": parts[1] if len(parts) > 1 and parts[1] else "critique",
        "variants": variants,
        "replace_by": "",
        "comment": "Terme ajouté par option E2E --lexicon-term",
        "contexts": [],
        "source": "e2e_cli",
    }


def build_extra_lexicon_from_args(args: argparse.Namespace) -> list[dict]:
    extra = []
    for raw in args.lexicon_term or []:
        item = _parse_cli_lexicon_term(raw)
        if item:
            extra.append(item)

    if args.lexicon_json:
        data = json.loads(args.lexicon_json.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("--lexicon-json doit contenir une liste JSON")
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("--lexicon-json contient une entrée non objet")
            term = str(raw.get("term", "")).strip()
            if not term:
                continue
            extra.append({
                "term": term,
                "category": raw.get("category", "terme_metier"),
                "priority": raw.get("priority", "critique"),
                "variants": raw.get("variants", []),
                "replace_by": raw.get("replace_by", ""),
                "comment": raw.get("comment", "Terme ajouté par --lexicon-json"),
                "contexts": raw.get("contexts", []),
                "source": raw.get("source", "e2e_json"),
            })
    return extra


# ─────────────────────────────────────────────────────────────────────────────
# Assertions
# ─────────────────────────────────────────────────────────────────────────────
def assert_file(path: Path, label: str) -> bool:
    if path.exists() and path.stat().st_size > 0:
        size_kb = path.stat().st_size / 1024
        ok(f"{label}: {path.relative_to(path.parents[1])} ({size_kb:.1f} Ko)")
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
        info(f"  clés: {preview_keys}")
    return data


def _job_audio_summary(job_id: str | None) -> dict:
    """Résumé audio compact persisté en base pour les campagnes de calibration STT.

    Best-effort : le JSON E2E doit rester productible hors contexte DB ou sur un
    job ancien. La frise `difficulty_map` n'est jamais embarquée ici ; elle reste
    dans `metadata/audio_preflight.json`.
    """
    if not job_id:
        return {}
    try:
        from transcria.jobs.store import JobStore

        job = JobStore.get_by_id(job_id)
        if job is None:
            return {}
        summary = job.get_extra_data().get("audio_summary") or {}
        return summary if isinstance(summary, dict) else {}
    except Exception:
        return {}


def _compact_difficulty_summary(preflight_data: dict) -> dict | None:
    summary = preflight_data.get("difficulty_summary")
    if isinstance(summary, dict):
        return {
            "windows": summary.get("windows"),
            "ok": summary.get("ok"),
            "suspect": summary.get("suspect"),
            "degrade": summary.get("degrade"),
            "degrade_ratio": summary.get("degrade_ratio"),
            "worst": summary.get("worst"),
        }

    difficulty_map = preflight_data.get("difficulty_map")
    if not isinstance(difficulty_map, list):
        return None

    counts = {"ok": 0, "suspect": 0, "degrade": 0}
    for window in difficulty_map:
        if not isinstance(window, dict):
            continue
        level = str(window.get("difficulty") or "ok")
        if level in counts:
            counts[level] += 1
    total = sum(counts.values())
    return {
        "windows": total,
        "ok": counts["ok"],
        "suspect": counts["suspect"],
        "degrade": counts["degrade"],
        "degrade_ratio": round(counts["degrade"] / total, 4) if total else 0.0,
        "worst": "degrade" if counts["degrade"] else ("suspect" if counts["suspect"] else "ok"),
    }


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _audio_corpus_snapshot(preflight_data: dict, job_audio_summary: dict) -> dict | None:
    """Données stables pour corréler difficulté audio, moteur STT et qualité.

    Le contrat reste compact et requêtable : uniquement scalaires ou agrégats.
    Les mesures par fenêtre et les segments complets restent dans les artefacts du
    job / bench pour éviter des JSON de campagne volumineux.
    """
    if not preflight_data and not job_audio_summary:
        return None

    source = job_audio_summary if isinstance(job_audio_summary, dict) else {}
    flags = source.get("flags")
    if flags is None:
        flags = preflight_data.get("flags") or []
    if not isinstance(flags, list):
        flags = [str(flags)]

    return {
        "schema_version": 1,
        "job_audio_summary": source or None,
        "risk_level": _first_present(source.get("risk_level"), preflight_data.get("risk_level")),
        "flags": flags,
        "duration_s": _first_present(source.get("duration_s"), preflight_data.get("duration_seconds")),
        "snr_db": _first_present(source.get("snr_db"), preflight_data.get("estimated_snr_db")),
        "bandwidth_95_hz": _first_present(source.get("bandwidth_95_hz"), preflight_data.get("bandwidth_95_hz")),
        "squim_global": _first_present(source.get("squim"), preflight_data.get("squim_global")),
        "dnsmos_global": _first_present(source.get("dnsmos"), preflight_data.get("dnsmos_global")),
        "difficulty_summary": _first_present(source.get("difficulty"), _compact_difficulty_summary(preflight_data)),
        "difficulty_map_windows": len(preflight_data.get("difficulty_map") or [])
        if isinstance(preflight_data.get("difficulty_map"), list)
        else 0,
    }


def _count_srt(path: Path) -> tuple[int, int]:
    """Retourne (nb_segments, nb_mots) d'un fichier SRT."""
    if not path.exists():
        return 0, 0
    content = path.read_text(encoding="utf-8", errors="replace")
    segments = 0
    words = 0
    in_text = False
    for line in content.splitlines():
        line = line.strip()
        if line.isdigit():
            segments += 1
            in_text = False
        elif "-->" in line:
            in_text = True
        elif in_text and line:
            words += len(line.split())
        elif not line:
            in_text = False
    return segments, words


_DAY_NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _active_schedule_payload(case: str, cfg: dict, args: argparse.Namespace) -> dict:
    from zoneinfo import ZoneInfo

    scheduling_cfg = cfg.get("workflow", {}).get("scheduling", {}) or {}
    timezone_name = str(scheduling_cfg.get("timezone", "Europe/Paris"))
    now = datetime.now(ZoneInfo(timezone_name))
    start = now - timedelta(minutes=5)
    end = now + timedelta(minutes=55)
    params = {}
    if case == "limit_concurrency":
        params["max_concurrent_jobs"] = max(1, int(args.schedule_limit_workers))
    action = "pause_queue" if case == "pause_then_release" else case
    return {
        "name": f"e2e-{case}-{int(time.time())}",
        "days": [_DAY_NAMES[now.weekday()]],
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "action": action,
        "action_params": params,
        "enabled": True,
    }


def run_schedule_case_probe(app, cfg: dict, args: argparse.Namespace, audio_file: Path, admin) -> None:
    if args.schedule_case == "none":
        return

    from transcria.jobs.filesystem import JobFilesystem
    from transcria.jobs.store import JobStore
    from transcria.queue.calendar import SchedulingCalendar, SchedulingWindowStore
    from transcria.queue.scheduler import QueueScheduler
    from transcria.queue.store import QueueStore
    from transcria.services.job_service import JobService

    step(f"Sonde agenda ({args.schedule_case})")
    timer_start("schedule_probe")

    window = SchedulingWindowStore.create(_active_schedule_payload(args.schedule_case, cfg, args))
    calendar = SchedulingCalendar(cfg.get("workflow", {}).get("scheduling", {}) or {})
    active = calendar.get_active_window()
    if active is None or active.id != window.id:
        RESULTS["schedule_probe"] = False
        raise RuntimeError(f"Fenêtre agenda non active pour le cas {args.schedule_case}")
    ok(f"Fenêtre active : {active.name} ({active.action})")

    probe_job_ids: list[str] = []
    stop_event = threading.Event()

    def create_probe_job(title: str) -> str:
        created = JobService.create(admin.id, title)
        probe_job_id = created["job_id"]
        probe_job_ids.append(probe_job_id)
        JobService.upload(
            probe_job_id,
            audio_file.read_bytes(),
            audio_file.name,
            cfg["storage"]["jobs_dir"],
        )
        analysis = JobService.analyze(probe_job_id, cfg["storage"]["jobs_dir"], cfg)
        if analysis.get("error"):
            raise RuntimeError(f"Analyse audio sonde agenda : {analysis['error']}")
        return probe_job_id

    def fake_process(job_id: str, audio_path: str, mode: str) -> None:
        stop_event.wait(timeout=10)

    scheduler = QueueScheduler(app, cfg, fake_process)
    try:
        if args.schedule_case == "pause_queue":
            probe_job_id = create_probe_job("E2E agenda pause_queue")
            QueueStore.enqueue(probe_job_id, mode=args.mode, vram_profile={"phases": {"stt": 0}})
            dispatched = scheduler._dispatch_iteration()
            entry = QueueStore.get_entry(probe_job_id)
            if dispatched != 0 or entry is None or entry.status != "waiting":
                RESULTS["schedule_probe"] = False
                raise RuntimeError("pause_queue aurait dû conserver le job en attente")
            ok("pause_queue vérifié : aucun dispatch, job conservé en attente")

        elif args.schedule_case == "pause_then_release":
            probe_job_id = create_probe_job("E2E agenda pause_then_release")
            QueueStore.enqueue(probe_job_id, mode=args.mode, vram_profile={"phases": {"stt": 0}})
            dispatched = scheduler._dispatch_iteration()
            entry = QueueStore.get_entry(probe_job_id)
            if dispatched != 0 or entry is None or entry.status != "waiting":
                RESULTS["schedule_probe"] = False
                raise RuntimeError("pause_then_release: le job aurait dû rester en attente pendant pause_queue")
            ok("pause_then_release : blocage initial vérifié")

            SchedulingWindowStore.delete(window.id)
            window = None
            dispatched = scheduler._dispatch_iteration()
            entry = QueueStore.get_entry(probe_job_id)
            if dispatched != 1 or entry is None or entry.status != "running":
                RESULTS["schedule_probe"] = False
                raise RuntimeError(
                    f"pause_then_release: attendu dispatch=1/running, obtenu dispatch={dispatched}, "
                    f"status={entry.status if entry else None}"
                )
            ok("pause_then_release vérifié : dispatch après suppression du créneau indisponible")

        elif args.schedule_case == "limit_concurrency":
            cfg.setdefault("workflow", {}).setdefault("execution", {})["max_concurrent_jobs"] = max(
                2,
                int(args.schedule_limit_workers) + 1,
            )
            scheduler.max_workers = int(cfg["workflow"]["execution"]["max_concurrent_jobs"])
            first_job_id = create_probe_job("E2E agenda limit_concurrency 1")
            second_job_id = create_probe_job("E2E agenda limit_concurrency 2")
            QueueStore.enqueue(first_job_id, mode=args.mode, vram_profile={"phases": {"stt": 0}})
            QueueStore.enqueue(second_job_id, mode=args.mode, vram_profile={"phases": {"stt": 0}})
            dispatched = scheduler._dispatch_iteration()
            expected = max(1, int(args.schedule_limit_workers))
            if dispatched != expected or scheduler.running_count != expected:
                RESULTS["schedule_probe"] = False
                raise RuntimeError(
                    f"limit_concurrency attendu={expected}, dispatched={dispatched}, running={scheduler.running_count}"
                )
            ok(f"limit_concurrency vérifié : {dispatched} dispatch pour {scheduler.max_workers} workers de base")

        elif args.schedule_case == "force_gpu":
            if not calendar.is_force_gpu_allowed():
                RESULTS["schedule_probe"] = False
                raise RuntimeError("force_gpu devrait être autorisé dans la fenêtre active")
            ok("force_gpu vérifié : fenêtre active et autorisation détectée")
            warn("Aucun kill GPU réel n'est déclenché par l'E2E standard")

        RESULTS["schedule_probe"] = True
        timer_end("schedule_probe")
    finally:
        stop_event.set()
        scheduler._executor.shutdown(wait=True, cancel_futures=True)
        for probe_job_id in probe_job_ids:
            entry = QueueStore.get_entry(probe_job_id)
            if entry is not None:
                db.session.delete(entry)
                db.session.flush()
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], probe_job_id)
            shutil.rmtree(fs.job_dir, ignore_errors=True)
            job = JobStore.get_by_id(probe_job_id)
            if job is not None:
                db.session.delete(job)
        if window is not None:
            SchedulingWindowStore.delete(window.id)
        db.session.commit()


def run_pipeline_via_queue_api(app, cfg: dict, args: argparse.Namespace, job_id: str) -> None:
    from transcria.auth.store import UserStore
    from transcria.jobs.models import JobState
    from transcria.jobs.store import JobStore
    from transcria.queue.store import QueueStore
    from transcria.workflow.transitions import get_execution_status

    client = app.test_client()
    admin = UserStore.get_by_username(cfg.get("auth", {}).get("first_admin_username", "admin"))
    if admin is None:
        raise RuntimeError("Utilisateur admin introuvable pour le client API E2E")
    with client.session_transaction() as session:
        session["_user_id"] = admin.id
        session["_fresh"] = True

    response = client.post(
        f"/api/jobs/{job_id}/process",
        json={"mode": args.mode, "priority": 10},
    )
    if response.status_code != 202:
        raise RuntimeError(f"POST /api/jobs/{job_id}/process: HTTP {response.status_code} {response.get_data(as_text=True)}")
    payload = response.get_json(silent=True) or {}
    ok(
        "API process acceptée : "
        f"execution_status={payload.get('execution_status')}, position={payload.get('queue_position')}"
    )

    deadline = time.time() + max(30, int(args.queue_api_timeout_s))
    last_status = None
    while time.time() < deadline:
        db.session.expire_all()
        job = JobStore.get_by_id(job_id)
        entry = QueueStore.get_entry(job_id)
        execution_status = get_execution_status(job) if job else "missing"
        status = (
            job.state if job else "missing",
            execution_status,
            entry.status if entry else "no_queue_entry",
        )
        if status != last_status:
            info(f"Queue API polling : state={status[0]}, execution={status[1]}, queue={status[2]}")
            last_status = status
        if job and job.state == JobState.COMPLETED.value:
            if entry is not None and entry.status not in {"done", "cancelled", "failed"}:
                raise RuntimeError(f"Entrée queue inattendue après COMPLETED: {entry.status}")
            ok("Queue API terminée : job completed, entrée queue finalisée")
            return
        if job and job.state in {JobState.FAILED.value, JobState.CANCELLED.value}:
            raise RuntimeError(f"Queue API terminée en échec: state={job.state}, error={job.error_message}")
        time.sleep(1)
    raise TimeoutError(f"Timeout queue API après {args.queue_api_timeout_s}s")


# ─────────────────────────────────────────────────────────────────────────────
# Résumé et JSON de sortie
# ─────────────────────────────────────────────────────────────────────────────
def _docx_theme_info(meeting_ctx: dict) -> dict:
    """Résout le thème DOCX appliqué et les champs type-spécifiques remplis.

    Permet au bench/E2E de vérifier que le bon thème et les bons champs
    sont sélectionnés selon le type de réunion, sans ouvrir le .docx.
    """
    meeting_type = meeting_ctx.get("meeting_type", "")
    try:
        from transcria.exports.docx_report import _get_theme
        theme = _get_theme(meeting_type)
        banner = theme.banner_text
        badge = theme.cover_badge
        is_default = banner == "COMPTE-RENDU DE TRANSCRIPTION" and not badge
    except Exception:
        banner, badge, is_default = "", "", True

    ts = meeting_ctx.get("type_specific_data") or {}
    ts_filled = {k: v for k, v in ts.items() if v is not None and str(v).strip()}
    return {
        "meeting_type": meeting_type,
        "banner_text": banner,
        "cover_badge": badge,
        "is_default_theme": is_default,
        "type_specific_fields_filled": sorted(ts_filled.keys()),
        "type_specific_count": len(ts_filled),
    }


def write_output_json(path: Path, args: argparse.Namespace, cfg: dict, fs) -> None:
    """Écrit le JSON de résultats structurés pour bench_audio.py."""
    workflow = cfg.get("workflow", {})
    job_id = RESULTS.get("job_id")
    job_audio_summary = _job_audio_summary(str(job_id) if job_id else None)

    # SRT stats
    srt_raw = fs.job_dir / "metadata" / "transcription.srt"
    srt_corr = fs.job_dir / "metadata" / "transcription_corrigee.srt"
    raw_segs, raw_words = _count_srt(srt_raw)
    _, corr_words = _count_srt(srt_corr)

    # Données de scène
    preflight_data = fs.load_json("metadata/audio_preflight.json") or {}
    scene_data = fs.load_json("metadata/audio_scene.json") or {}
    quality_data = fs.load_json("metadata/audio_quality_decision.json") or {}
    whisper_hotwords_data = fs.load_json("metadata/whisper_hotwords.json") or {}
    cohere_biasing_data = fs.load_json("metadata/cohere_lexicon_biasing.json") or {}
    cohere_tf5_data = fs.load_json("metadata/cohere_tf5.json") or {}
    granite_data = fs.load_json("metadata/granite.json") or {}
    parakeet_data = fs.load_json("metadata/parakeet.json") or {}
    transcription_metadata = fs.load_json("metadata/transcription_metadata.json") or {}
    transcription_segments = fs.load_json("metadata/transcription_segments.json") or []
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    reliability_counts = {}
    if isinstance(transcription_segments, list):
        for segment in transcription_segments:
            level = segment.get("reliability")
            if level:
                reliability_counts[level] = reliability_counts.get(level, 0) + 1
    stats_data = fs.load_json("speakers/speaker_stats.json") or {}
    _raw_spk = stats_data.get("speakers") or []
    spk_list = [
        ({"speaker_id": s} if isinstance(s, str) else s) for s in _raw_spk
    ]

    # Timings propres
    clean_timings = {}
    for k, v in TIMINGS.items():
        if isinstance(v, float) and v < 86400:
            clean_timings[f"{k}_s"] = round(v, 2)

    payload = {
        "combo_id": args.combo_id,
        "run_started_at": RUN_STARTED_AT,
        "audio_file": args.audio.name,
        "audio_path": str(args.audio),

        # Options de ce run
        "stt_backend": args.stt_backend,
        "effective_stt_backend": transcription_metadata.get("backend"),
        "whisper_model_size": args.whisper_model_size if args.stt_backend == "whisper" else None,
        "cohere_tf5_data": cohere_tf5_data or None,
        "granite_data": granite_data or None,
        "parakeet_data": parakeet_data or None,
        "mode": args.mode,
        "skip_llm": args.skip_llm,
        "skip_diarization": args.skip_diarization,
        "audio_scene": workflow.get("audio_scene", {}).get("enabled", False),
        "audio_preflight": workflow.get("audio_preflight", {}).get("enabled", False),
        "source_separation": workflow.get("source_separation", {}).get("enabled", False),
        "force_source_separation": workflow.get("source_separation", {}).get("force", False),
        "audio_normalization": workflow.get("audio_normalization", {}).get("enabled", False),
        "weak_voice_normalization": workflow.get("audio_normalization", {}).get("weak_voice", {}).get("enabled", True),
        "audio_denoise": workflow.get("audio_denoise", {}).get("enabled", False),
        "force_audio_denoise": workflow.get("audio_denoise", {}).get("force", False),
        "segment_reliability": workflow.get("segment_reliability", {}).get("enabled", True),
        "micro_chunk_merge": workflow.get("pyannote_chunking", {}).get("merge_micro_chunks", True),
        "vad_hysteresis": workflow.get("vad", {}).get("hysteresis_enabled", False),
        "scene_filter": workflow.get("audio_scene_filter", {}).get("enabled", False),
        "whisper_lexicon_hotwords": cfg.get("whisper", {}).get("lexicon_hotwords", {}).get("enabled", False),
        "cohere_lexicon_biasing": cfg.get("cohere", {}).get("lexicon_biasing", {}).get("enabled", False),
        "lexicon_terms_cli": len(args.lexicon_term or []),
        "lexicon_json": str(args.lexicon_json) if args.lexicon_json else None,
        "gpu": args.gpu,
        "arbitrage_port": args.arbitrage_port,
        "schedule_case": args.schedule_case,
        "schedule_limit_workers": args.schedule_limit_workers,
        "process_via_api": args.process_via_api,
        "config_overrides": {
            item.split("=", 1)[0].strip(): _parse_override_value(item.split("=", 1)[1].strip())
            for item in args.config_override
            if "=" in item
        },

        # Résultat global
        "schema_version": 2,
        "status": "ok" if not [v for v in RESULTS.values() if v is False] else "fail",
        "errors": ERRORS,

        # Timings
        "timings": clean_timings,

        # VRAM
        "vram_peak_mb": _peak_vram_mb(
            GPU_SNAPSHOTS,
            gpu_id=os.environ.get("CUDA_VISIBLE_DEVICES", "")
            if os.environ.get("CUDA_VISIBLE_DEVICES", "")
            and "," not in os.environ.get("CUDA_VISIBLE_DEVICES", "")
            else None,
        ),
        "vram_snapshots": [
            {
                "label": s["label"],
                "gpus": [
                    {"id": g["id"], "mem_used_mb": g["mem_used_mb"], "mem_free_mb": g["mem_free_mb"]}
                    for g in (s.get("gpus") or [])
                ],
            }
            for s in GPU_SNAPSHOTS
        ],

        # SRT
        "srt": {
            "raw_segments": raw_segs,
            "raw_words": raw_words,
            "corrected_exists": srt_corr.exists(),
            "corrected_words": corr_words if srt_corr.exists() else None,
            "raw_path": str(srt_raw) if srt_raw.exists() else None,
            "corrected_path": str(srt_corr) if srt_corr.exists() else None,
        },

        # Artefacts optionnels
        "artifacts": {
            "audio_preflight": (fs.job_dir / "metadata" / "audio_preflight.json").exists(),
            "audio_scene": (fs.job_dir / "metadata" / "audio_scene.json").exists(),
            "audio_denoise": (fs.job_dir / "metadata" / "audio_denoise.json").exists(),
            "transcription_metadata": (fs.job_dir / "metadata" / "transcription_metadata.json").exists(),
            "whisper_hotwords": (fs.job_dir / "metadata" / "whisper_hotwords.json").exists(),
            "cohere_lexicon_biasing": (fs.job_dir / "metadata" / "cohere_lexicon_biasing.json").exists(),
            "source_separation": (fs.job_dir / "input" / "vocals.wav").exists(),
            "scene_filter": (fs.job_dir / "input" / "scene_filtered.wav").exists(),
            "normalization": (fs.job_dir / "input" / "normalized.wav").exists(),
            "diarization_checkpoint": (fs.job_dir / "speakers" / "diarization_checkpoint.json").exists(),
            "granite": (fs.job_dir / "metadata" / "granite.json").exists(),
            "parakeet": (fs.job_dir / "metadata" / "parakeet.json").exists(),
            "zip_export": bool(list((fs.job_dir / "exports").glob("*.zip"))) if (fs.job_dir / "exports").exists() else False,
            "docx_export": bool(list((fs.job_dir / "exports").glob("*.docx"))) if (fs.job_dir / "exports").exists() else False,
        },

        # Pré-diagnostic audio
        "audio_preflight_data": {
            "risk_level": preflight_data.get("risk_level"),
            "flags": preflight_data.get("flags") or [],
            "rms": preflight_data.get("rms"),
            "peak": preflight_data.get("peak"),
            "estimated_snr_db": preflight_data.get("estimated_snr_db"),
            "bandwidth_95_hz": preflight_data.get("bandwidth_95_hz"),
            "bandwidth_99_hz": preflight_data.get("bandwidth_99_hz"),
            "silence_ratio": preflight_data.get("silence_ratio"),
        } if preflight_data else None,

        # Corpus STT : résumé compact pour calibration difficulté ↔ moteur ↔ qualité.
        "audio_corpus": _audio_corpus_snapshot(preflight_data, job_audio_summary),

        # Données de scène (si disponibles)
        "audio_scene_data": {
            "speech_ratio": scene_data.get("speech_ratio"),
            "music_ratio": scene_data.get("music_ratio"),
            "noise_ratio": scene_data.get("noise_ratio"),
            "has_music": scene_data.get("has_music"),
            "has_noise": scene_data.get("has_noise"),
            "problem_segments": len(scene_data.get("problem_segments") or []),
            "gender_segments": len(scene_data.get("gender_segments") or []),
        } if scene_data else None,

        # Décision qualité audio
        "quality_decision": {
            "level": quality_data.get("level"),
            "reasons": quality_data.get("reasons") or [],
        } if quality_data else None,

        "transcription_metadata": {
            "backend": transcription_metadata.get("backend"),
            "chunking_mode": transcription_metadata.get("chunking_mode"),
            "gpu_index": transcription_metadata.get("gpu_index"),
            "language": transcription_metadata.get("language"),
            "segments": transcription_metadata.get("segments"),
            "speaker_count": transcription_metadata.get("speaker_count"),
            "vad_final_enabled": transcription_metadata.get("vad_final_enabled"),
            "chunk_metrics": transcription_metadata.get("chunk_metrics"),
        } if transcription_metadata else None,

        "segment_reliability_counts": reliability_counts,
        "whisper_hotwords_data": {
            "enabled": whisper_hotwords_data.get("enabled"),
            "candidate_terms": whisper_hotwords_data.get("candidate_terms"),
            "injected_terms": whisper_hotwords_data.get("injected_terms"),
            "excluded_terms": whisper_hotwords_data.get("excluded_terms"),
            "excluded_by_priority": whisper_hotwords_data.get("excluded_by_priority"),
            "excluded_by_duplicate": whisper_hotwords_data.get("excluded_by_duplicate"),
            "excluded_by_budget": whisper_hotwords_data.get("excluded_by_budget"),
            "max_tokens": whisper_hotwords_data.get("max_tokens"),
            "token_count": whisper_hotwords_data.get("token_count"),
            "token_count_method": whisper_hotwords_data.get("token_count_method"),
            "terms": whisper_hotwords_data.get("terms") or [],
            "has_existing_hotwords": whisper_hotwords_data.get("has_existing_hotwords"),
        } if whisper_hotwords_data else None,
        "cohere_lexicon_biasing_data": {
            "enabled": cohere_biasing_data.get("enabled"),
            "candidate_terms": cohere_biasing_data.get("candidate_terms"),
            "injected_terms": cohere_biasing_data.get("injected_terms"),
            "excluded_terms": cohere_biasing_data.get("excluded_terms"),
            "excluded_by_priority": cohere_biasing_data.get("excluded_by_priority"),
            "excluded_by_duplicate": cohere_biasing_data.get("excluded_by_duplicate"),
            "excluded_by_budget": cohere_biasing_data.get("excluded_by_budget"),
            "boost": cohere_biasing_data.get("boost"),
            "start_boost": cohere_biasing_data.get("start_boost"),
            "max_prefix_tokens": cohere_biasing_data.get("max_prefix_tokens"),
            "terms": cohere_biasing_data.get("terms") or [],
        } if cohere_biasing_data else None,

        # Locuteurs
        "speakers": {
            "count": len(spk_list),
            "gender_attributed": len([s for s in spk_list if s.get("gender")]),
        },

        # Données structurées enrichies (extraction LLM section 8b du prompt)
        "structured_data": {
            "parse_status": meeting_ctx.get("structured_data_parse_status", "missing"),
            "parse_warning": meeting_ctx.get("structured_data_parse_warning", ""),
            "decisions_count": len((meeting_ctx.get("structured_data") or {}).get("decisions") or []),
            "actions_count": len((meeting_ctx.get("structured_data") or {}).get("actions") or []),
            "votes_count": len((meeting_ctx.get("structured_data") or {}).get("votes") or []),
            "resolutions_count": len((meeting_ctx.get("structured_data") or {}).get("resolutions") or []),
            "has_prochaine_date": bool((meeting_ctx.get("structured_data") or {}).get("prochaine_date")),
        },

        # Rendu DOCX par type : thème appliqué + champs type-spécifiques
        "docx_theme": _docx_theme_info(meeting_ctx),

        # Références
        "job_id": RESULTS.get("job_id"),
        "job_dir": str(fs.job_dir) if fs else None,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    ok(f"JSON de résultats écrit : {path}")


def print_summary(args: argparse.Namespace) -> None:
    section("RESUME FINAL")
    effective_backend = ""
    job_dir = RESULTS.get("job_dir")
    if job_dir:
        metadata_path = Path(job_dir) / "metadata" / "transcription_metadata.json"
        if metadata_path.exists():
            try:
                effective_backend = json.loads(metadata_path.read_text(encoding="utf-8")).get("backend", "")
            except Exception:
                effective_backend = ""

    print(f"  Job ID       : {RESULTS.get('job_id', 'N/A')}")
    print(f"  Audio        : {args.audio}")
    print(f"  STT          : {args.stt_backend}"
          + (f" ({args.whisper_model_size})" if args.stt_backend == "whisper" else ""))
    if effective_backend and effective_backend != args.stt_backend:
        print(f"  STT effectif : {effective_backend}")
    if job_dir:
        hotwords_path = Path(job_dir) / "metadata" / "whisper_hotwords.json"
        if hotwords_path.exists():
            try:
                hotwords = json.loads(hotwords_path.read_text(encoding="utf-8"))
                print(
                    "  Hotwords     : "
                    f"{hotwords.get('injected_terms', 0)}/{hotwords.get('candidate_terms', 0)} injectés"
                )
            except Exception:
                pass
        cohere_biasing_path = Path(job_dir) / "metadata" / "cohere_lexicon_biasing.json"
        if cohere_biasing_path.exists():
            try:
                biasing = json.loads(cohere_biasing_path.read_text(encoding="utf-8"))
                print(
                    "  Biasing Cohere: "
                    f"{biasing.get('injected_terms', 0)}/{biasing.get('candidate_terms', 0)} injectés"
                )
            except Exception:
                pass
    print(f"  Mode         : {args.mode}")
    print(f"  Combo ID     : {args.combo_id or '—'}")
    print(f"  Étapes       : {STEP}")

    print("\n  Timings :")
    total = 0.0
    for name, elapsed in TIMINGS.items():
        if isinstance(elapsed, float) and elapsed < 86400:
            print(f"    {name:28s} {elapsed:6.1f}s")
            total += elapsed
    print(f"    {'TOTAL':28s} {total:6.1f}s")

    print("\n  GPU timeline :")
    for snapshot in GPU_SNAPSHOTS:
        first_gpu = (snapshot.get("gpus") or [None])[0]
        if first_gpu:
            print(
                f"    {snapshot['label'][:44]:44s} "
                f"GPU{first_gpu['id']} {first_gpu['mem_used_mb']:>6} Mo utilisés, "
                f"{len(snapshot.get('processes') or []):>2} proc"
            )
        else:
            print(f"    {snapshot['label'][:44]:44s} nvidia-smi indisponible")

    failed = [k for k, v in RESULTS.items() if v is False]
    skipped = [k for k, v in RESULTS.items() if v is None]
    ok_count = sum(1 for v in RESULTS.values() if v is True)
    print(f"\n  Résultats : {ok_count} OK / {len(failed)} échec(s) / {len(skipped)} ignoré(s)")
    if failed:
        print(f"  Échecs    : {failed}")
    if ERRORS:
        print(f"\n  Erreurs détaillées :")
        for err in ERRORS:
            print(f"    - {err}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    args = parse_args()
    if args.process_via_api and args.schedule_case != "none":
        fail("--process-via-api ne peut pas être combiné avec --schedule-case dans ce script")
        return 1

    # Validation du fichier audio
    audio_file = args.audio
    if not audio_file.exists():
        fail(f"Fichier audio introuvable: {audio_file}")
        return 1

    audio_size_mb = audio_file.stat().st_size / 1024 / 1024

    print(f"\n{'#' * 72}")
    print(f"  TranscrIA — E2E workflow production")
    print(f"  {'─' * 68}")
    print(f"  Audio        : {audio_file} ({audio_size_mb:.1f} Mo)")
    print(f"  Démarré      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.combo_id:
        print(f"  Combo ID     : {args.combo_id}")
    if args.gpu:
        print(f"  GPU(s)       : {args.gpu} (CUDA_VISIBLE_DEVICES)")
    print(f"{'#' * 72}")

    section("État initial GPU")
    gpu_checkpoint("initial")

    vram_monitor = _VRAMMonitor(interval_s=3.0)
    vram_monitor.start()

    # ── Initialisation ───────────────────────────────────────────────────────
    step("Initialisation Flask / DB / Config")
    timer_start("init")

    cfg = load_config()
    apply_e2e_config(cfg, args)
    apply_config_overrides(cfg, args.config_override)
    set_config(cfg)

    print_effective_config(cfg, args)

    app = create_app()
    app.config.update({"TESTING": True})

    with app.app_context():
        db.create_all()
        from transcria.auth.store import UserStore
        UserStore.ensure_admin(cfg)
        admin = UserStore.get_by_username("admin")
        if admin is None:
            fail("Utilisateur admin introuvable après ensure_admin()")
            return 1
        ok("Application Flask, DB et admin prêts")

    RESULTS["init"] = True
    timer_end("init")

    # Variable pour le nettoyage final
    fs = None
    job_id = None

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

        try:
            run_schedule_case_probe(app, cfg, args, audio_file, admin)

            # ── Création / upload / analyse ──────────────────────────────────
            step("Création / upload / analyse (JobService)")
            timer_start("prepare")

            created = JobService.create(admin.id, args.job_title)
            job_id = created["job_id"]
            RESULTS["job_id"] = job_id
            ok(f"Job créé : {job_id}")

            upload = JobService.upload(
                job_id, audio_file.read_bytes(), audio_file.name,
                cfg["storage"]["jobs_dir"],
            )
            ok(f"Upload : {upload.get('original_filename')} ({upload.get('size_bytes')} octets)")

            analysis = JobService.analyze(job_id, cfg["storage"]["jobs_dir"], cfg)
            if analysis.get("error"):
                raise RuntimeError(f"Analyse audio : {analysis['error']}")

            duration_s = analysis.get("duration_seconds", 0)
            ok(f"Analyse audio : {duration_s:.1f}s, codec={analysis.get('codec')}, "
               f"sr={analysis.get('sample_rate')}Hz, ch={analysis.get('channels')}")

            job = JobStore.get_by_id(job_id)
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job_id)
            RESULTS["job_dir"] = str(fs.job_dir)
            audio_path = fs.get_original_audio_path()
            if audio_path is None:
                raise RuntimeError("Chemin audio original introuvable après upload")

            RESULTS["prepare"] = True
            timer_end("prepare")

            # ── Phase résumé ─────────────────────────────────────────────────
            if not args.skip_summary:
                step("Résumé production (WorkflowRunner.run_summary)")
                timer_start("summary")
                gpu_checkpoint("avant-summary")

                runner = WorkflowRunner(JobStore, cfg)
                summary_result = runner.run_summary(job, str(audio_path), cfg)

                gpu_checkpoint("apres-summary")

                if summary_result.get("error") and not summary_result.get("transcript_text"):
                    raise RuntimeError(f"run_summary : {summary_result['error']}")

                job = JobStore.get_by_id(job_id)
                ok(f"État après résumé : {job.state}")
                ok(f"Transcription rapide : {summary_result.get('segment_count', 0)} segment(s)")

                if not args.skip_llm:
                    assert_file(fs.job_dir / "summary" / "summary.md", "summary.md")

                RESULTS["summary"] = True
                timer_end("summary")
            else:
                info("Phase résumé ignorée (--skip-summary)")
                RESULTS["summary"] = None
                # Avancer l'état manuellement pour permettre la suite
                if job.state in (JobState.UPLOADED.value, JobState.ANALYZED.value):
                    JobStore.update_state(job.id, JobState.SUMMARY_DONE)
                    job = JobStore.get_by_id(job_id)

            # ── Contexte ─────────────────────────────────────────────────────
            step("Contexte réunion")
            timer_start("context")

            context_payload = build_context_payload(fs, args.job_title)
            MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], context_payload)
            if job.state == JobState.SUMMARY_DONE.value:
                JobStore.update_state(job.id, JobState.CONTEXT_DONE)
            job = JobStore.get_by_id(job_id)
            ok(f"Contexte sauvegardé, état={job.state}")

            RESULTS["context"] = True
            timer_end("context")

            # ── Participants ─────────────────────────────────────────────────
            step("Participants (détectés automatiquement)")
            timer_start("participants")

            participants = build_participants_from_speakers(fs)
            saved_participants = ParticipantsManager.save(
                job, cfg["storage"]["jobs_dir"], participants
            )
            if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
                JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
            job = JobStore.get_by_id(job_id)
            ok(f"Participants créés : {len(saved_participants)} (noms vides volontairement)")

            RESULTS["participants"] = True
            timer_end("participants")

            # ── Lexique ──────────────────────────────────────────────────────
            step("Lexique utilisateur")
            timer_start("lexicon")

            lexicon = build_lexicon_from_summary(fs)
            extra_lexicon = build_extra_lexicon_from_args(args)
            if extra_lexicon:
                lexicon.extend(extra_lexicon)
                ok(f"Lexique E2E ajouté par options : {len(extra_lexicon)} terme(s)")
            LexiconManager.save(job, cfg["storage"]["jobs_dir"], lexicon)
            advance_preprocessing_state(job.id, job.state)
            JobContextBuilder.build(job, cfg["storage"]["jobs_dir"], cfg)
            job = JobStore.get_by_id(job_id)
            ok(f"Lexique sauvegardé : {len(lexicon)} terme(s), état={job.state}")

            RESULTS["lexicon"] = True
            timer_end("lexicon")

            # ── Mapping SPEAKER_XX ───────────────────────────────────────────
            step("Mapping SPEAKER_XX → participants")
            timer_start("mapping")

            participants = ParticipantsManager.get(job, cfg["storage"]["jobs_dir"])
            mapping = build_mapping_from_speakers(fs, participants)
            if mapping:
                SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
                JobContextBuilder.build(job, cfg["storage"]["jobs_dir"], cfg)
                meeting_ctx = fs.load_json("context/meeting_context.json") or {}
                speaker_roles_llm = meeting_ctx.get("speaker_roles_llm", {})
                if speaker_roles_llm:
                    WorkflowRunner._apply_speaker_roles(
                        fs, speaker_roles_llm,
                        logging.getLogger("e2e.speaker_roles"),
                    )
                    ok(f"Rôles LLM appliqués : {list(speaker_roles_llm.keys())}")
                ok(f"Mapping sauvegardé : {len(mapping)} speaker(s)")
            else:
                warn("Aucun SPEAKER_XX détecté — mapping ignoré")

            JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
            job = JobStore.get_by_id(job_id)

            RESULTS["mapping"] = True
            timer_end("mapping")

            # ── Pipeline complet ─────────────────────────────────────────────
            step(f"Traitement complet (PipelineService, mode={args.mode})")
            timer_start("pipeline")
            gpu_checkpoint("avant-pipeline")

            if args.process_via_api:
                run_pipeline_via_queue_api(app, cfg, args, job.id)
                result = {}
            else:
                pipeline = PipelineService(cfg)
                result = pipeline.run_process(job, str(audio_path), args.mode)

            gpu_checkpoint("apres-pipeline")

            if result.get("error"):
                raise RuntimeError(f"[{result.get('step')}] {result['error']}")

            job = JobStore.get_by_id(job_id)
            ok(f"Pipeline terminé, état={job.state}, mode={args.mode}")

            RESULTS["pipeline"] = True
            timer_end("pipeline")

            # ── Vérification des artefacts ───────────────────────────────────
            step("Vérification des artefacts")
            timer_start("verify")

            audio_ext = args.audio.suffix  # e.g. ".mp3", ".m4a", ".wav"
            expected = [
                f"input/original{audio_ext}",
                "metadata/audio_analysis.json",
                "context/meeting_context.json",
                "context/participants.json",
                "context/session_lexicon.json",
                "context/job_context.yaml",
                "metadata/transcription.srt",
                "metadata/transcription_metadata.json",
                "quality/quality_report.json",
            ]
            if not args.skip_summary:
                expected.extend([
                    "summary/quick_transcript.txt",
                    "summary/summary.json",
                    "summary/summary.md",
                ])
            if mapping:
                expected.extend(["speakers/speaker_stats.json", "speakers/speaker_mapping.json"])
            if not args.skip_llm:
                expected.extend([
                    "metadata/transcription_corrigee.srt",
                    "metadata/correction_report.md",
                ])

            found = sum(1 for p in expected if assert_file(fs.job_dir / p, p))

            exports = list((fs.job_dir / "exports").glob("*.zip")) if (fs.job_dir / "exports").exists() else []
            if exports:
                ok(f"Export ZIP : {exports[0].name} ({exports[0].stat().st_size / 1024:.0f} Ko)")
                found += 1
            else:
                warn("Export ZIP absent")

            RESULTS["verify"] = found == len(expected) + 1
            timer_end("verify")

            # ── Artefacts optionnels ─────────────────────────────────────────
            section("Artefacts optionnels (selon config)")
            optional_artifacts = [
                ("metadata/audio_preflight.json",        "Pré-diagnostic audio"),
                ("metadata/audio_quality_decision.json", "Décision qualité audio"),
                ("metadata/transcription_metadata.json", "Métadonnées transcription"),
                ("metadata/audio_scene.json",            "Analyse de scène audio"),
                ("metadata/audio_scene_filter.json",     "Filtrage scène audio"),
                ("metadata/audio_normalization.json",    "Normalisation audio"),
                ("metadata/audio_denoise.json",          "Débruitage audio expérimental"),
                ("input/vocals.wav",                     "Piste vocale (Demucs)"),
                ("input/scene_filtered.wav",             "Audio filtré scène"),
                ("input/normalized.wav",                 "Audio normalisé"),
                ("input/denoised.wav",                   "Audio débruité"),
                ("speakers/diarization_checkpoint.json", "Checkpoint diarisation"),
                ("speakers/speaker_embeddings.json",     "Embeddings locuteurs"),
                ("summary/diarization_context.md",       "Contexte diarisation LLM"),
            ]
            for rel_path, label in optional_artifacts:
                assert_file(fs.job_dir / rel_path, label)

            # ── Détail prétraitements ────────────────────────────────────────
            section("Détail des prétraitements audio")

            preflight_data = print_json_artifact(fs, "metadata/audio_preflight.json", "audio_preflight.json")
            if isinstance(preflight_data, dict):
                ok(
                    f"audio_preflight : risk={preflight_data.get('risk_level')}, "
                    f"flags={preflight_data.get('flags')}, "
                    f"rms={preflight_data.get('rms')}, "
                    f"snr={preflight_data.get('estimated_snr_db')}, "
                    f"bw99={preflight_data.get('bandwidth_99_hz')}"
                )
            elif not args.disable_audio_preflight:
                RESULTS["audio_preflight"] = False
                fail("audio_preflight activé mais metadata/audio_preflight.json absent")

            scene_data = print_json_artifact(fs, "metadata/audio_scene.json", "audio_scene.json")
            if isinstance(scene_data, dict):
                ok(
                    f"audio_scene : "
                    f"speech={scene_data.get('speech_ratio')}, "
                    f"music={scene_data.get('music_ratio')}, "
                    f"noise={scene_data.get('noise_ratio')}, "
                    f"problem_segments={len(scene_data.get('problem_segments') or [])}"
                )

            filter_data = print_json_artifact(
                fs, "metadata/audio_scene_filter.json", "audio_scene_filter.json"
            )
            if isinstance(filter_data, dict):
                if filter_data.get("preserve_timeline") is True:
                    ok(f"audio_scene_filter : timeline préservée, "
                       f"{len(filter_data.get('intervals') or [])} intervalle(s)")
                else:
                    RESULTS["audio_scene_filter_timeline"] = False
                    fail("audio_scene_filter : preserve_timeline absent ou false")
            elif args.enable_scene_filter:
                warn("audio_scene_filter forcé mais aucun filtrage appliqué (pas de zones filtrables ?)")

            normalization_data = print_json_artifact(
                fs, "metadata/audio_normalization.json", "audio_normalization.json"
            )
            if isinstance(normalization_data, dict):
                if normalization_data.get("preserve_timeline") is True:
                    ok(f"audio_normalization : timeline préservée, "
                       f"filtres={normalization_data.get('filters')}")
                else:
                    RESULTS["audio_normalization_timeline"] = False
                    fail("audio_normalization : preserve_timeline absent ou false")
            elif args.enable_audio_normalization:
                RESULTS["audio_normalization"] = False
                fail("audio_normalization forcée mais metadata/audio_normalization.json absent")

            denoise_data = print_json_artifact(
                fs, "metadata/audio_denoise.json", "audio_denoise.json"
            )
            if isinstance(denoise_data, dict):
                if denoise_data.get("preserve_timeline") is True:
                    ok(f"audio_denoise : timeline préservée, "
                       f"filtres={denoise_data.get('filters')}")
                else:
                    RESULTS["audio_denoise_timeline"] = False
                    fail("audio_denoise : preserve_timeline absent ou false")
            elif args.force_audio_denoise:
                RESULTS["audio_denoise"] = False
                fail("--force-audio-denoise actif mais metadata/audio_denoise.json absent")

            segs = fs.load_json("metadata/transcription_segments.json") or []
            reliability_counts = {}
            if isinstance(segs, list):
                for seg in segs:
                    level = seg.get("reliability")
                    if level:
                        reliability_counts[level] = reliability_counts.get(level, 0) + 1
            if reliability_counts:
                ok(f"segment_reliability : {reliability_counts}")
            elif not args.disable_segment_reliability:
                warn("segment_reliability activé mais aucun champ reliability trouvé")

            transcription_metadata = print_json_artifact(
                fs, "metadata/transcription_metadata.json", "transcription_metadata.json"
            )
            if isinstance(transcription_metadata, dict):
                backend_effectif = transcription_metadata.get("backend")
                ok(
                    f"transcription_metadata : backend={backend_effectif}, "
                    f"chunking={transcription_metadata.get('chunking_mode')}, "
                    f"segments={transcription_metadata.get('segments')}, "
                    f"vad_final={transcription_metadata.get('vad_final_enabled')}"
                )
                if backend_effectif and backend_effectif != args.stt_backend:
                    warn(
                        f"Backend demandé={args.stt_backend}, backend effectif={backend_effectif}. "
                        "Cela peut être normal si quality_transcription force un backend."
                    )

            vocals_path = fs.job_dir / "input" / "vocals.wav"
            if vocals_path.exists():
                ok(f"Demucs exécuté : vocals.wav ({vocals_path.stat().st_size / 1024:.0f} Ko)")
            elif args.force_source_separation:
                RESULTS["force_source_separation"] = False
                fail("--force-source-separation actif mais vocals.wav absent — "
                     "vérifier que Demucs est installé (pip install demucs)")

            # ── Genre par locuteur ───────────────────────────────────────────
            section("Genre vocal par locuteur (attribution acoustique)")

            scene_path = fs.job_dir / "metadata" / "audio_scene.json"
            if scene_path.exists():
                scene = json.loads(scene_path.read_text())
                gender_segs = scene.get("gender_segments") or []
                info(f"gender_segments dans audio_scene.json : {len(gender_segs)} segment(s)")
                if gender_segs:
                    total_female = sum(
                        s["end"] - s["start"] for s in gender_segs if s.get("label") == "female"
                    )
                    total_male = sum(
                        s["end"] - s["start"] for s in gender_segs if s.get("label") == "male"
                    )
                    ok(f"Segments genre : {total_female:.1f}s féminin / {total_male:.1f}s masculin")
                else:
                    warn("Pas de segments genre (detect_gender désactivé ou audio trop court)")
            else:
                warn("audio_scene.json absent — analyse de scène désactivée ou échouée")

            stats_data = fs.load_json("speakers/speaker_stats.json") or {}
            _raw_spk2 = stats_data.get("speakers") or []
            spk_list = [
                ({"speaker_id": s} if isinstance(s, str) else s) for s in _raw_spk2
            ]
            gender_filled = [s for s in spk_list if s.get("gender")]
            info(f"Locuteurs avec genre attribué : {len(gender_filled)}/{len(spk_list)}")
            for spk in spk_list:
                spk_id = spk.get("speaker_id", "?")
                gender = spk.get("gender") or "(non attribué)"
                mapped = spk.get("mapped_name") or ""
                label = (
                    f"{mapped} ({spk_id})"
                    if mapped and not mapped.upper().startswith("SPEAKER_")
                    else spk_id
                )
                info(f"  {label:30s}  genre={gender}")

            # ── Participants finaux ──────────────────────────────────────────
            section("Participants finaux")
            for participant in ParticipantsManager.get(job, cfg["storage"]["jobs_dir"]):
                info(json.dumps(
                    {"id": participant.get("id"), "name": participant.get("name"),
                     "role": participant.get("role")},
                    ensure_ascii=False,
                ))

            # ── SRT stats ────────────────────────────────────────────────────
            section("Statistiques SRT")
            srt_raw = fs.job_dir / "metadata" / "transcription.srt"
            srt_corr = fs.job_dir / "metadata" / "transcription_corrigee.srt"
            raw_segs, raw_words = _count_srt(srt_raw)
            info(f"SRT brut      : {raw_segs} segments, {raw_words} mots")
            if srt_corr.exists():
                _, corr_words = _count_srt(srt_corr)
                info(f"SRT corrigé   : {corr_words} mots")

        except Exception as exc:
            fail("E2E interrompu par une exception", exc)
            RESULTS["e2e_exception"] = False
            traceback.print_exc()

        finally:
            vram_monitor.stop()

            # ── JSON de sortie ───────────────────────────────────────────────
            if args.output_json and fs:
                try:
                    write_output_json(args.output_json, args, cfg, fs)
                except Exception as exc:
                    warn(f"Impossible d'écrire le JSON de résultats : {exc}")

            # ── Nettoyage ────────────────────────────────────────────────────
            has_failure = bool([v for v in RESULTS.values() if v is False])
            keep_job = args.keep or (args.keep_on_error and has_failure)

            if fs and job_id:
                if keep_job:
                    section("Job conservé")
                    print(f"  Job ID     : {job_id}")
                    print(f"  Répertoire : {fs.job_dir}")
                else:
                    section("Nettoyage")
                    shutil.rmtree(fs.job_dir, ignore_errors=True)
                    ok(f"Job supprimé : {fs.job_dir}")

    print_summary(args)

    failed = [k for k, v in RESULTS.items() if v is False]
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        fail("E2E interrompu (exception non capturée)", exc)
        RESULTS["e2e"] = False
        traceback.print_exc()
        print_summary(argparse.Namespace(
            audio=Path("?"), combo_id=None, gpu=None,
            stt_backend="?", whisper_model_size="?", mode="?",
        ))
        raise SystemExit(1)

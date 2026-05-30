#!/usr/bin/env python3
"""Smoke test E2E du STT distant contre un VRAI serveur (vLLM, SGLang, …).

Contrairement aux tests unitaires/intégration (faux serveurs), ce script vise un
serveur réellement chargé en VRAM et fait passer un vrai audio par le
RemoteTranscriber — exactement le chemin du pipeline en mode frontale.

`fallback_local` est désactivé : si le serveur est absent ou répond mal, le script
ÉCHOUE bruyamment (sinon une bascule locale masquerait le problème qu'on teste).

Pré-requis : lancer le serveur, p.ex.
    STT_GPU=3 STT_PORT=8003 ./scripts/launch_stt_cohere.sh

Exemples :
    python scripts/smoke_remote_stt.py                                  # cohere @ <ip-lan>:8003
    python scripts/smoke_remote_stt.py --backend whisper --port 8005
    python scripts/smoke_remote_stt.py --host 127.0.0.1 --audio tests/test2.mp3
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import time
from pathlib import Path

_DEFAULT_PORTS = {"cohere": 8003, "whisper": 8005}
_DEFAULT_MODELS = {"cohere": "cohere-transcribe", "whisper": "whisper-large-v3"}


def _primary_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        return "127.0.0.1" if ip.startswith("127.") else ip
    except OSError:
        return "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=sorted(_DEFAULT_PORTS), default="cohere")
    ap.add_argument("--host", default=None, help="IP/host du serveur (défaut : IP LAN principale)")
    ap.add_argument("--port", type=int, default=None, help="port HTTP (défaut selon backend)")
    ap.add_argument("--model", default=None, help="served-model-name (défaut selon backend)")
    ap.add_argument("--audio", default="tests/test2.mp3", help="fichier audio (MP3 converti en WAV)")
    ap.add_argument("--language", default="fr")
    ap.add_argument("--api-key", default=None, help="clé API si le serveur en exige une")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("smoke_remote_stt")

    host = args.host or _primary_lan_ip()
    port = args.port or _DEFAULT_PORTS[args.backend]
    model = args.model or _DEFAULT_MODELS[args.backend]
    url = f"http://{host}:{port}/v1"

    audio = Path(args.audio)
    if not audio.is_file():
        log.error("Audio introuvable : %s", audio)
        return 2

    # Repo importable quand lancé depuis la racine.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from transcria.stt.transcriber_factory import create_transcriber

    cfg = {
        "inference": {
            "mode": "remote",
            "stt": {
                "fallback_local": False,  # échec bruyant : on teste le distant, pas la bascule
                "response_format": "verbose_json",
                "retries": 1,
                "timeout_s": 300,
                "auth": {"api_key": args.api_key or ""},
                "backends": {args.backend: {"url": url, "model": model}},
            },
        }
    }

    log.info("Cible : %s (backend=%s, model=%s)", url, args.backend, model)
    transcriber = create_transcriber(cfg, backend=args.backend)
    if type(transcriber).__name__ != "RemoteTranscriber":
        log.error("La factory n'a PAS choisi RemoteTranscriber (%s) — config incorrecte.",
                  type(transcriber).__name__)
        return 1
    if not transcriber.load():
        log.error("Serveur injoignable au /v1/models — lancez-le d'abord (cf. en-tête du script).")
        return 1

    log.info("Transcription de %s …", audio.name)
    t0 = time.time()
    segments = transcriber.transcribe(audio, language=args.language)
    elapsed = time.time() - t0

    errors = [s for s in segments if s.get("error")]
    if errors:
        log.error("Échec distant : %s", errors[0]["error"])
        return 1
    if not segments:
        log.error("Aucun segment renvoyé (réponse vide ?).")
        return 1

    print(f"\n=== {len(segments)} segment(s) en {elapsed:.2f}s ===")
    for s in segments[:5]:
        print(f"  [{s['start']:7.2f} → {s['end']:7.2f}]  {s['text']}")
    if len(segments) > 5:
        print(f"  … (+{len(segments) - 5} autres)")
    full = " ".join(s["text"] for s in segments if s.get("text"))
    print(f"\nTexte ({len(full)} car.) : {full[:300]}{'…' if len(full) > 300 else ''}")
    print(f"\n✅ Smoke OK — {url} a transcrit l'audio via RemoteTranscriber.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""GATE MANUEL — le bot rejoint une vraie salle Jitsi et on regarde le PCM arriver.

Ce que ce gate PROUVE (et que la CI ne peut pas prouver) : un vrai Chromium rejoint la
réunion, le payload de capture s'injecte, l'interception WebRTC produit du PCM, et ce PCM
traverse le pont jusqu'à la session live. Il n'utilise **aucun STT** : le transcripteur est
un COMPTEUR de frames. On sépare volontairement « la capture marche » de « le STT marche ».

Prérequis : `pip install -r requirements-connectors.txt` (playwright + websockets) et
`playwright install chromium`. Jitsi (meet.jit.si) est public et ne demande pas de compte.

Usage :
    python scripts/gate_bot_jitsi.py https://meet.jit.si/ma-salle-de-test
    python scripts/gate_bot_jitsi.py https://meet.jit.si/ma-salle --show   # fenêtre visible
    python scripts/gate_bot_jitsi.py https://meet.jit.si/ma-salle --seconds 60

Pendant ce temps : ouvre la MÊME URL dans ton navigateur/téléphone, rejoins et PARLE.
Le script affiche les frames reçues par participant. Zéro frame = la capture ne marche pas
(sélecteurs Jitsi, admission, ou WebCodecs indisponible) — c'est justement ce qu'on veut savoir.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connector_service.bot.platforms.jitsi import JitsiDriver  # noqa: E402
from connector_service.bot.runner import run_bot_session  # noqa: E402
from connector_service.contract import ExternalMeetingOccurrence  # noqa: E402


class FrameCounter:
    """Transcripteur factice : compte les frames PCM par participant et trace la progression.
    Remplacé par le vrai moteur live (Kyutai/WhisperLiveKit) une fois la capture validée."""

    uses_local_agreement = False

    def __init__(self) -> None:
        self.per_participant: Counter[str] = Counter()
        self.bytes_total = 0

    async def stream(self, frames):
        async for frame in frames:
            self.per_participant[frame.participant_id] += 1
            self.bytes_total += len(frame.payload)
            total = sum(self.per_participant.values())
            if total % 50 == 0:                       # trace périodique, sans noyer la console
                print(f"  … {total} frames | {self.bytes_total // 1024} Ko | "
                      f"participants={dict(self.per_participant)}", flush=True)
        return
        yield  # pragma: no cover  (générateur : aucun segment, on ne transcrit pas ici)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Gate manuel du bot de réunion sur Jitsi")
    parser.add_argument("meeting_url", help="URL de la salle (ex. https://meet.jit.si/ma-salle)")
    parser.add_argument("--seconds", type=float, default=45.0,
                        help="durée max de la session (défaut 45 s)")
    parser.add_argument("--show", action="store_true", help="fenêtre visible (sinon headless)")
    parser.add_argument("--port", type=int, default=8791, help="port du pont PCM local")
    parser.add_argument("--name", default="TranscrIA-bot", help="nom affiché du bot")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    occurrence = ExternalMeetingOccurrence(
        provider="bot", provider_account_id="gate-local",
        external_occurrence_id=args.meeting_url.rstrip("/").rsplit("/", 1)[-1])

    counter = FrameCounter()
    driver = JitsiDriver(f"ws://127.0.0.1:{args.port}", headless=not args.show,
                         alone_poll_s=5.0, max_duration_s=args.seconds)

    print(f"→ salle    : {args.meeting_url}")
    print(f"→ pont PCM : ws://127.0.0.1:{args.port}")
    print(f"→ mode     : {'fenêtre visible' if args.show else 'headless'}")
    print("→ REJOINS LA MÊME SALLE ET PARLE pendant que ça tourne…\n", flush=True)

    try:
        outcome, _segments = await asyncio.wait_for(
            run_bot_session(args.meeting_url, occurrence, driver, counter,
                            display_name=args.name, bridge_port=args.port),
            timeout=args.seconds + 120)
    except asyncio.TimeoutError:
        print("\n❌ ÉCHEC : la session n'a pas rendu la main (blocage).")
        return 2

    total = sum(counter.per_participant.values())
    print("\n────────── RÉSULTAT DU GATE ──────────")
    print(f"admission      : {'OK' if outcome.admitted else 'REFUSÉE/TIMEOUT'}")
    print(f"fin de session : {outcome.reason}")
    print(f"frames PCM     : {total}  ({counter.bytes_total // 1024} Ko)")
    print(f"participants   : {dict(counter.per_participant) or '—'}")

    if not outcome.admitted:
        print("\n❌ Le bot n'a pas été admis : vérifier l'URL, le lobby, les sélecteurs de join.")
        return 1
    if total == 0:
        print("\n❌ Admis mais AUCUN PCM : la capture ne remonte pas.")
        print("   Pistes : WebCodecs (MediaStreamTrackProcessor) indisponible dans ce Chromium,")
        print("   payload non injecté, ou aucune piste audio distante (personne ne parlait).")
        return 1
    print("\n✅ CAPTURE VALIDÉE : le PCM par participant atteint la session live.")
    print("   Prochaine étape : remplacer FrameCounter par le vrai moteur STT live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

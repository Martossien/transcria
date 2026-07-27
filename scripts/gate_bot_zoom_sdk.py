#!/usr/bin/env python
"""GATE — bot ZOOM par le Meeting SDK NATIF, contre une VRAIE réunion.

Ce que ce gate prouve, et que rien d'autre ne peut prouver :
  1. l'authentification aboutit avec un vrai Client ID / Client Secret ;
  2. le bot ENTRE dans la réunion sans ouvrir de micro (aucun bip pour les participants) ;
  3. l'audio arrive PAR PARTICIPANT, avec de l'ÉNERGIE réelle — pas seulement des frames
     qui coulent (piège déjà rencontré : un flux « actif » ne transportant que des zéros) ;
  4. les locuteurs sont NOMMÉS — l'apport décisif du SDK sur le pilote navigateur, qui
     documentait cette identité comme non résoluble ;
  5. optionnellement, la façade TranscrIA transcrit en direct.

⚠ À exécuter DANS le conteneur : hors de lui, le SDK plante par segfault faute de D-Bus et de
PulseAudio (diagnostiqué). Les identifiants passent par l'environnement, jamais par la ligne
de commande — une option serait lisible dans la liste des processus.

    docker build -f Dockerfile.zoom-sdk -t transcria-zoom-sdk:latest .
    docker run --rm \
      -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
      --entrypoint /usr/local/bin/zoom-sdk-entrypoint \
      -v "$PWD/scripts:/app/scripts:ro" transcria-zoom-sdk:latest \
      python3 -u /app/scripts/gate_bot_zoom_sdk.py --meeting "578 629 7113" --seconds 60

Prérequis côté Zoom : une app de type « Meeting SDK » créée sur LE MÊME COMPTE que celui qui
héberge la réunion. Ce régime ne demande ni revue de l'app, ni jeton ZAK/OBF — c'est ce qui
rend ce gate exécutable sans démarche préalable.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connector_service.bot.zoom_sdk import exit_immediately, parse_zoom_invite  # noqa: E402
from connector_service.contract import ExternalMeetingOccurrence  # noqa: E402
from connector_service.live._demux import DemuxFrameSource  # noqa: E402
from connector_service.live.facade_client import facade_transcriber  # noqa: E402
from connector_service.live.facade_stt import FacadeTranscriber  # noqa: E402
from connector_service.live.media import LiveAudioProvider  # noqa: E402
from connector_service.live.session import LiveSession  # noqa: E402
from connector_service.live.zoom_sdk_state import ZoomSdkPhase  # noqa: E402
from connector_service.live.zoom_sdk_transport import (  # noqa: E402
    SAMPLING_RATE_32K,
    ZoomSdkError,
    zoom_sdk_demux_source,
)

VOICE_THRESHOLD = 500        # même seuil que les autres gates : au-dessus du bruit de fond


class _FrameCounter:
    """Transcripteur factice qui MESURE ce qui arrive vraiment.

    Compter les frames ne suffit pas : un flux peut couler en ne transportant que des zéros.
    On mesure donc aussi la crête et le nombre de frames réellement sonores, PAR LOCUTEUR
    NOMMÉ — ce qui vérifie du même coup l'attribution.
    """

    uses_local_agreement = False

    def __init__(self) -> None:
        self.frames_by_speaker: Counter[str] = Counter()
        self.loud_by_speaker: Counter[str] = Counter()
        self.peak = 0

    def observe(self, frame) -> None:
        # `AudioFrame` (contrat) porte le nom sous `participant_display_name` — pas
        # `participant_name`, qui est le champ de `RawFrame`, en amont de la conversion.
        label = frame.participant_display_name or frame.participant_id or "?"
        self.frames_by_speaker[label] += 1
        count = len(frame.payload) // 2
        if not count:
            return
        peak = max(abs(v) for v in struct.unpack(f"<{count}h", frame.payload[:count * 2]))
        self.peak = max(self.peak, peak)
        if peak > VOICE_THRESHOLD:
            self.loud_by_speaker[label] += 1

    async def stream(self, frames):
        async for frame in frames:
            self.observe(frame)
        return
        yield  # pragma: no cover — générateur sans émission


class _Tee:
    """Mesure l'énergie captée PUIS délègue au vrai moteur STT."""

    def __init__(self, counter: _FrameCounter, inner) -> None:
        self._counter, self._inner = counter, inner
        self.uses_local_agreement = inner.uses_local_agreement

    def stream(self, frames):
        async def _tee():
            async for frame in frames:
                self._counter.observe(frame)
                yield frame
        return self._inner.stream(_tee())


async def main() -> int:
    # Journalisation à INFO : sans elle, seuls les AVERTISSEMENTS s'affichent, et l'on ne
    # voit ni les changements de phase, ni l'obtention du droit d'enregistrement, ni une
    # reconnexion en cours de réunion. Un gate qui n'expose pas ce qu'il fait pendant qu'il
    # le fait oblige à attendre la fin pour comprendre — constaté à ses dépens.
    logging.basicConfig(level=os.environ.get("GATE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Gate du bot Zoom (Meeting SDK natif)")
    parser.add_argument("--meeting", required=True,
                        help="numéro de réunion ou lien d'invitation")
    parser.add_argument("--passcode", default=os.environ.get("ZOOM_PASSCODE", ""))
    parser.add_argument("--name", default="TranscrIA (gate)")
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="durée de CAPTURE, une fois le bot admis")
    parser.add_argument("--join-timeout-s", type=float, default=300.0,
                        help="attente d'admission (salle d'attente) avant d'abandonner")
    parser.add_argument("--sampling-rate-hz", type=int, default=SAMPLING_RATE_32K)
    parser.add_argument("--transcribe", help="URL TranscrIA pour transcrire en direct")
    parser.add_argument("--token-file", help="fichier du jeton d'API TranscrIA")
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("❌ ZOOM_CLIENT_ID et ZOOM_CLIENT_SECRET requis dans l'environnement.")
        return 3

    meeting_number, invite_passcode = parse_zoom_invite(args.meeting)
    passcode = args.passcode or invite_passcode

    counter = _FrameCounter()
    transcriber = counter
    if args.transcribe:
        token = Path(args.token_file).read_text().strip() if args.token_file else ""
        transcriber = _Tee(counter, FacadeTranscriber(
            facade_transcriber(args.transcribe, token, language=args.language)))

    phases: list[ZoomSdkPhase] = []
    print(f"→ réunion : {meeting_number}  | code : {'oui' if passcode else 'non'}")
    print(f"→ nom affiché : {args.name}  | débit : {args.sampling_rate_hz} Hz")
    print("→ le bot entre MICRO ET CAMÉRA COUPÉS : aucun son ne doit être émis dans la réunion.")
    print("→ ⚠ ACCEPTEZ la fenêtre « Autoriser l'enregistrement » qui s'affichera côté hôte :")
    print("   sans ce droit, Zoom ne délivre AUCUN audio brut (sur un compte gratuit, c'est")
    print("   la seule voie — le jeton d'enregistrement local y est indisponible).")

    source = DemuxFrameSource(zoom_sdk_demux_source(
        client_id, client_secret, meeting_number,
        display_name=args.name, passcode=passcode,
        sampling_rate_hz=args.sampling_rate_hz,
        admission_timeout_s=args.join_timeout_s,
        on_phase=phases.append))
    provider = LiveAudioProvider("zoom", source)

    occurrence = ExternalMeetingOccurrence(
        provider="zoom", provider_account_id="gate",
        external_occurrence_id=meeting_number)

    segments: list = []
    session = LiveSession(transcriber, on_final=segments.append)
    # Le plafond global couvre l'ATTENTE D'ADMISSION *puis* la capture. Les confondre
    # faisait abandonner le bot pendant qu'il patientait légitimement en salle d'attente,
    # alors que `--seconds` est censé désigner la seule durée de capture.
    try:
        await asyncio.wait_for(session.run(provider, occurrence),
                               timeout=args.join_timeout_s + args.seconds)
    except asyncio.TimeoutError:
        pass                                              # durée du gate atteinte
    except ZoomSdkError as exc:
        print(f"\n❌ ÉCHEC ZOOM : {exc}")
        return 1

    total = sum(counter.frames_by_speaker.values())
    loud = sum(counter.loud_by_speaker.values())
    named = [label for label in counter.frames_by_speaker
             if not label.startswith("participant-")]

    print("\n────────── RÉSULTAT ──────────")
    print(f"phases traversées : {' → '.join(p.value for p in phases) or '—'}")
    print(f"frames captées    : {total} | crête {counter.peak}/32767 | sonores {loud}")
    print("par locuteur      :")
    for label, count in counter.frames_by_speaker.most_common():
        print(f"    {label:30s} {count:6d} frames, {counter.loud_by_speaker[label]:6d} sonores")
    for segment in segments:
        print(f"  [{segment.speaker or '?'}] {segment.text}")

    if ZoomSdkPhase.ACTIVE not in phases:
        print("\n❌ Le bot n'est JAMAIS entré dans la réunion.")
        return 1
    if total == 0:
        print("\n❌ Entré, mais AUCUNE frame audio — l'abonnement à l'audio brut ne produit rien.")
        return 1
    if loud == 0:
        print("\n⚠️  Flux capté mais SILENCIEUX : personne n'a parlé, ou le flux ne "
              "transporte que des zéros. Refaire en parlant dans la réunion.")
        return 1
    if not named:
        print("\n⚠️  Audio capté et sonore, mais AUCUN locuteur nommé : le registre des "
              "participants ne se remplit pas — c'est l'apport principal du SDK, à corriger.")
        return 1

    print(f"\n✅ BOT ZOOM (SDK NATIF) VALIDÉ : audio réel capté et attribué "
          f"à {len(named)} locuteur(s) nommé(s).")
    return 0


if __name__ == "__main__":
    # Même raison que pour le bot : le SDK segfaute à la finalisation de l'interpréteur,
    # ce qui ferait passer un gate RÉUSSI pour un échec.
    exit_immediately(asyncio.run(main()))

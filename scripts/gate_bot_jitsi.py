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
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connector_service.bot.browser import CHROMIUM_ARGS  # noqa: E402
from connector_service.bot.platforms.jitsi import JitsiDriver  # noqa: E402
from connector_service.bot.runner import run_bot_session  # noqa: E402
from connector_service.contract import ExternalMeetingOccurrence  # noqa: E402


class FakeParticipant:
    """2e navigateur qui rejoint la MÊME salle et émet du son (tonalité du périphérique
    factice de Chromium). Permet un gate AUTONOME : plus besoin d'un humain qui parle."""

    def __init__(self, meeting_url: str, name: str = "Participant-Test", *,
                 ignore_https_errors: bool = False, audio_file: str | None = None) -> None:
        self._url = meeting_url
        self._name = name
        self._ignore_https_errors = ignore_https_errors
        # Fichier WAV joué comme micro : indispensable pour tester avec de la VRAIE PAROLE.
        # La tonalité du périphérique factice ne convient pas — le traitement audio des
        # plateformes (suppression de bruit) écrase les sons stationnaires.
        self._audio_file = audio_file
        self._pw = None
        self._browser = None

    async def join(self) -> bool:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        args = list(CHROMIUM_ARGS)
        if self._audio_file:
            args.append(f"--use-file-for-fake-audio-capture={self._audio_file}")
        self._browser = await self._pw.chromium.launch(headless=True, args=args)
        page = await self._browser.new_page(
            ignore_https_errors=self._ignore_https_errors)
        await page.goto(self._url, timeout=45000, wait_until="domcontentloaded")
        box = page.get_by_placeholder("Enter your name")
        try:
            await box.first.wait_for(state="visible", timeout=20000)
            await box.first.fill(self._name)
        except Exception:  # noqa: BLE001 — écran de prejoin absent
            pass
        btn = page.locator("[data-testid='prejoin.joinMeeting']")
        if await btn.count():
            await btn.first.click()
            await asyncio.sleep(8)
            return True
        return False

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()


class FrameCounter:
    """Transcripteur factice : compte les frames PCM par participant et trace la progression.
    Remplacé par le vrai moteur live (Kyutai/WhisperLiveKit) une fois la capture validée."""

    uses_local_agreement = False

    def __init__(self) -> None:
        self.per_participant: Counter[str] = Counter()
        self.names: dict[str, str] = {}     # participant → nom affiché (mappage plateforme)
        self.bytes_total = 0
        self.peak = 0                      # amplitude max rencontrée (0..32767)
        self.loud_frames = 0               # frames au-dessus du seuil de « vrai son »
        self.peak_by_participant: dict[str, int] = {}
        self.loud_by_participant: Counter[str] = Counter()

    @staticmethod
    def _peak_amplitude(payload: bytes) -> int:
        """Amplitude crête d'une frame PCM s16le. Distingue du VRAI SON d'un flux silencieux :
        un compteur de frames seul ne le dit pas (WebRTC émet aussi pendant les silences)."""
        count = len(payload) // 2
        if count == 0:
            return 0
        return max(abs(v) for v in struct.unpack(f"<{count}h", payload[:count * 2]))

    async def stream(self, frames):
        async for frame in frames:
            self.per_participant[frame.participant_id] += 1
            if frame.participant_display_name:
                self.names[frame.participant_id] = frame.participant_display_name
            self.bytes_total += len(frame.payload)
            peak = self._peak_amplitude(frame.payload)
            self.peak = max(self.peak, peak)
            pid = frame.participant_id
            self.peak_by_participant[pid] = max(self.peak_by_participant.get(pid, 0), peak)
            if peak > 500:                            # ~1,5 % de l'échelle : au-dessus du bruit
                self.loud_frames += 1
                self.loud_by_participant[pid] += 1
            total = sum(self.per_participant.values())
            if total % 50 == 0:                       # trace périodique, sans noyer la console
                print(f"  … {total} frames | {self.bytes_total // 1024} Ko | "
                      f"crête={self.peak} | sonores={self.loud_frames} | "
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
    parser.add_argument("--insecure", action="store_true",
                        help="accepte un certificat auto-signé (instance auto-hébergée)")
    parser.add_argument("--participant-audio", metavar="FICHIER.wav",
                        help="voix jouée par le participant factice (WAV 48 kHz mono) — "
                             "de la vraie parole, bien plus représentative qu'une tonalité")
    parser.add_argument("--fake-participant", action="store_true",
                        help="lance un 2e navigateur qui rejoint et émet du son "
                             "(gate AUTONOME, sans humain)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    occurrence = ExternalMeetingOccurrence(
        provider="bot", provider_account_id="gate-local",
        external_occurrence_id=args.meeting_url.rstrip("/").rsplit("/", 1)[-1])

    counter = FrameCounter()
    driver = JitsiDriver(f"ws://127.0.0.1:{args.port}", headless=not args.show,
                         alone_poll_s=5.0, max_duration_s=args.seconds,
                         ignore_https_errors=args.insecure)

    print(f"→ salle    : {args.meeting_url}")
    print(f"→ pont PCM : ws://127.0.0.1:{args.port}")
    print(f"→ mode     : {'fenêtre visible' if args.show else 'headless'}")

    participant = None
    if args.fake_participant:
        print("→ participant factice : connexion (émet la tonalité du périph. factice)…",
              flush=True)
        participant = FakeParticipant(args.meeting_url,
                                      ignore_https_errors=args.insecure,
                                      audio_file=args.participant_audio)
        joined = await participant.join()
        print(f"→ participant factice : {'DANS LA SALLE' if joined else 'ÉCHEC DU JOIN'}\n",
              flush=True)
    else:
        print("→ REJOINS LA MÊME SALLE ET PARLE pendant que ça tourne…\n", flush=True)

    try:
        outcome, _segments = await asyncio.wait_for(
            run_bot_session(args.meeting_url, occurrence, driver, counter,
                            display_name=args.name, bridge_port=args.port),
            timeout=args.seconds + 120)
    except asyncio.TimeoutError:
        print("\n❌ ÉCHEC : la session n'a pas rendu la main (blocage).")
        return 2
    finally:
        if participant is not None:
            await participant.close()

    total = sum(counter.per_participant.values())
    print("\n────────── RÉSULTAT DU GATE ──────────")
    print(f"admission      : {'OK' if outcome.admitted else 'REFUSÉE/TIMEOUT'}")
    print(f"fin de session : {outcome.reason}")
    print(f"frames PCM     : {total}  ({counter.bytes_total // 1024} Ko)")
    print(f"participants   : {dict(counter.per_participant) or '—'}")
    print(f"noms résolus   : {counter.names or '— (aucun nom : mappage à vérifier)'}")
    for pid in counter.per_participant:
        print(f"   • {pid:28s} crête={counter.peak_by_participant.get(pid, 0):5d} "
              f"sonores={counter.loud_by_participant.get(pid, 0)}")
    print(f"amplitude crête: {counter.peak} / 32767")
    print(f"frames sonores : {counter.loud_frames} "
          f"({(100 * counter.loud_frames // total) if total else 0} %)")

    if not outcome.admitted:
        print("\n❌ Le bot n'a pas été admis : vérifier l'URL, le lobby, les sélecteurs de join.")
        return 1
    if total == 0:
        print("\n❌ Admis mais AUCUN PCM : la capture ne remonte pas.")
        print("   Pistes : WebCodecs (MediaStreamTrackProcessor) indisponible dans ce Chromium,")
        print("   payload non injecté, ou aucune piste audio distante (personne ne parlait).")
        return 1
    if counter.loud_frames == 0:
        print("\n⚠️  Flux capté mais SILENCIEUX (aucune frame au-dessus du bruit) :")
        print("   le micro était-il coupé ? Personne n'a parlé ? Le son est-il bien transmis ?")
        return 1
    print("\n✅ CAPTURE VALIDÉE : de l'audio RÉEL par participant atteint la session live.")
    print("   Prochaine étape : remplacer FrameCounter par le vrai moteur STT live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

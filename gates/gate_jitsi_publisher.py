"""Gate Jitsi AUTOMATISÉ — un participant NAVIGATEUR qui joue un fichier audio.

Pendant du publisher Visio (`gate_visio_publisher.py`) pour l'autre plateforme
auto-hébergée : Chromium (Playwright, déjà dans l'image `transcria-bot`) rejoint la salle
avec un MICRO FACTICE alimenté par un WAV — les mêmes drapeaux que le bot de production
(`--use-fake-device-for-media-stream`), plus `--use-file-for-fake-audio-capture` que le
bot n'utilise pas (lui capte, il ne publie rien).

⚠ Le fichier DOIT être un WAV PCM 16 bits (Chromium refuse le mp3 en capture factice) —
`--use-file-for-fake-audio-capture=<wav>%noloop` ou sans %noloop pour boucler jusqu'à la
fin de la session (utile : couvrir le cycle de claim du runner, cf. GATE_LOOPS côté Visio).

Usage :
  sudo docker run --rm --network host --shm-size=1g -v $PWD/gates:/g -v $PWD/tests:/t \\
    --entrypoint python transcria-bot:latest /g/gate_jitsi_publisher.py \\
    https://localhost:8493/ma-salle "Testeur A" /t/test1.wav 120
"""
from __future__ import annotations

import asyncio
import sys

# Mêmes options muettes que le bot de prod (cf. platforms/jitsi.py) — l'audio publié
# vient du fichier, aucune caméra, aucun périphérique réel.
_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]


async def publish(meeting_url: str, display_name: str, wav_path: str,
                  duration_s: float) -> None:
    from playwright.async_api import async_playwright

    fragment = ("#config.prejoinConfig.enabled=false"
                "&config.p2p.enabled=false"
                "&config.startWithAudioMuted=false"
                "&config.startWithVideoMuted=true"
                f"&userInfo.displayName=%22{display_name.replace(' ', '%20')}%22")
    url = meeting_url.split("#")[0] + fragment
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[*_ARGS, f"--use-file-for-fake-audio-capture={wav_path}"],
            ignore_default_args=["--mute-audio"])
        page = await browser.new_page(ignore_https_errors=True)
        page.on("console", lambda m: None)
        print(f"[{display_name}] rejoint {url}", flush=True)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Jitsi en HEADLESS ne crée AUCUNE piste locale tout seul (constaté : store
        # `features/base/tracks` vide alors que getUserMedia rend « Fake Default Audio
        # Input » live). On la crée et on la publie EXPLICITEMENT par son API, puis on
        # VÉRIFIE — sans piste publiée, le banc ne prouverait rien.
        await asyncio.sleep(15)
        res = await page.evaluate("""async () => {
            try {
                const tracks = await JitsiMeetJS.createLocalTracks({devices: ['audio']});
                await APP.conference.useAudioStream(tracks[0]);
                return {created: tracks.length};
            } catch (e) { return {err: e.name + ': ' + (e.message || '')}; }
        }""")
        await asyncio.sleep(5)
        published = await page.evaluate(
            "() => (APP.store.getState()['features/base/tracks'] || [])"
            ".some(t => t.local && t.mediaType === 'audio' && !t.muted)")
        print(f"[{display_name}] micro {'PUBLIÉ' if published else 'NON publié'} ({res})",
              flush=True)
        await asyncio.sleep(duration_s)
        print(f"[{display_name}] durée écoulée — sortie", flush=True)
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(publish(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])))

"""Plomberie COMMUNE des bots vers le parcours produit — extraite de `bot/cli.py` (lot V0,
`docs/VISIO_ZOOM_RUNNER.md` D-V1).

Trois briques, identiques quel que soit le bot (navigateur Jitsi, SDK Zoom, client LiveKit
Visio) : les ÉTATS relayés au runner (`{"bot_event": …}`), les tours du SUIVI EN DIRECT
(`{"bot_caption": …}` — provisoire par contrat, ADR-001 D5), et le RATTACHEMENT de
l'enregistrement au job planifié (mix + pistes séparées en flux depuis le disque, manifeste
v2). Une correction ici profite aux trois bots — c'est la raison d'être du module.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os

logger = logging.getLogger(__name__)


def json_event_emitter():
    """BOT_EVENTS=json (vague 4) : une ligne JSON par transition d'état sur stdout — le
    meeting-runner les relaie au portail (états « salle d'attente », « en réunion » sur la
    carte du job) sans parser les logs. Mapping vers les événements de /v1/meetings/events."""
    mapping = {"joining": "joining", "waiting_admission": "waiting_admission",
               "active": "in_meeting"}

    def emit(state) -> None:
        event = mapping.get(getattr(state, "value", str(state)))
        if event:
            print(json.dumps({"bot_event": event}), flush=True)
    return emit


def json_caption_emitter():
    """Suivi en direct (vague 5, lot C) : une ligne JSON par TOUR FINAL sur stdout — le
    meeting-runner les regroupe et les POSTe à /v1/meetings/<sid>/captions. Provisoire par
    contrat (ADR-001 D5) : le pipeline batch reste la référence."""

    def emit(seg) -> None:
        print(json.dumps({"bot_caption": {
            "start": round(float(seg.start), 3), "end": round(float(seg.end), 3),
            "speaker": (seg.speaker or "")[:120], "text": seg.text[:500],
        }}, ensure_ascii=False), flush=True)
    return emit


async def ingest_recording(transcria_url: str, token: str, occurrence, recording,
                           job_id: str) -> None:
    """Rattache l'enregistrement au job planifié — best-effort JOURNALISÉ : un échec ici ne
    change pas le code de sortie (la réunion a bien eu lieu), mais il se voit."""
    from connector_service.bridge import JobsApiBridge
    from connector_service.transports import RequestsTransport

    if os.environ.get("BOT_EVENTS") == "json":
        print(json.dumps({"bot_event": "ingesting"}), flush=True)
    provider = os.environ.get("TRANSCRIA_PROVIDER") or "bot"
    opened: list = []
    try:
        # Tout part en FLUX depuis le disque (vague 5, lot A) : le mix normalisé ET une
        # part par piste séparée — jamais un enregistrement complet en RAM.
        mix_path = recording.mixer.to_wav_file()
        track_parts = {}
        for ref, path in recording.track_files().items():
            fh = open(path, "rb")
            opened.append(fh)
            track_parts[ref] = (f"{ref}.wav", fh)
        mix_fh = open(mix_path, "rb")
        opened.append(mix_fh)
        bridge = JobsApiBridge(transcria_url, token or "", RequestsTransport())
        result = await bridge.ingest_recording(
            mix_fh,
            f"{occurrence.external_occurrence_id}.wav",
            idempotency_key=f"bot|{occurrence.external_occurrence_id}|{job_id}",
            provider=provider,
            external_meeting_id=occurrence.external_occurrence_id,
            participants_manifest=recording.to_manifest(provider),
            job_id=job_id,
            track_files=track_parts or None)
        logger.info("Enregistrement rattaché au job %s (HTTP %s, %.0f s d'audio, %d piste(s))",
                    job_id, result.status_code, recording.mixer.duration_s, len(track_parts))
    except Exception:  # noqa: BLE001
        logger.exception("rattachement de l'enregistrement impossible (job %s)", job_id)
    finally:
        for fh in opened:
            with contextlib.suppress(OSError):
                fh.close()

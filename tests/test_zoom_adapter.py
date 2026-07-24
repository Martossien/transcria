"""A2 — adaptateur Zoom Cloud Recording, contre une fixture recording.completed réelle."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from connector_service.providers.zoom import (
    ZoomEventError,
    ZoomRecording,
    ZoomRecordingAdapter,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "zoom_recording_completed.json").read_text())


def test_parse_selectionne_la_piste_audio():
    rec = ZoomRecording.from_payload(FIXTURE)
    assert rec.meeting_uuid == "aB3dEf9/gHiJkLmNoPq=="
    assert rec.file_id == "file-audio-001"         # audio_only choisi, pas la vidéo
    assert rec.download_url.endswith("file-audio-001")
    assert rec.download_token.startswith("eyJ")


def test_repli_video_si_pas_d_audio_only():
    payload = copy.deepcopy(FIXTURE)
    files = payload["payload"]["object"]["recording_files"]
    del files[0]  # retire l'audio → il ne reste que la vidéo MP4
    rec = ZoomRecording.from_payload(payload)
    assert rec.file_id == "file-video-002"


def test_uuid_manquant_erreur():
    payload = copy.deepcopy(FIXTURE)
    payload["payload"]["object"]["uuid"] = ""
    with pytest.raises(ZoomEventError, match="uuid"):
        ZoomRecording.from_payload(payload)


def test_aucune_piste_audio_erreur():
    payload = copy.deepcopy(FIXTURE)
    payload["payload"]["object"]["recording_files"] = [
        {"id": "t", "file_type": "TRANSCRIPT", "status": "completed",
         "download_url": "https://x", "recording_type": "audio_transcript"}]
    with pytest.raises(ZoomEventError, match="audio"):
        ZoomRecording.from_payload(payload)


class TestAdapter:
    adapter = ZoomRecordingAdapter()

    def test_occurrence_sur_uuid(self):
        occ = self.adapter.to_occurrence(ZoomRecording.from_payload(FIXTURE))
        assert occ.provider == "zoom"
        assert occ.external_occurrence_id == "aB3dEf9/gHiJkLmNoPq=="
        assert occ.provider_account_id == "host-XYZ789"

    def test_artifact_pointe_download_url(self):
        art = self.adapter.to_artifact(ZoomRecording.from_payload(FIXTURE))
        assert art.storage_uri == "https://zoom.us/rec/download/file-audio-001"
        assert art.media_type == "audio/mp4" and art.artifact_type == "recording"

    def test_dedup_composite_uuid_et_fichier(self):
        rec = ZoomRecording.from_payload(FIXTURE)
        k = self.adapter.dedup_key(rec)
        assert k == "zoom|host-XYZ789|aB3dEf9/gHiJkLmNoPq==|file-audio-001"

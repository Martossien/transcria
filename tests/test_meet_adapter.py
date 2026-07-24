"""A4 — adaptateur Google Meet REST v2, contre une fixture de ressource recording réelle."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from connector_service.providers.meet import (
    MeetRecording,
    MeetRecordingAdapter,
    MeetRecordingError,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "meet_recording.json").read_text())


def test_parse_recording():
    rec = MeetRecording.from_recording(FIXTURE)
    assert rec.conference_record_id == "conf-abc" and rec.recording_id == "rec-xyz"
    assert rec.drive_file_id == "drive-file-999" and rec.space == "spaces/space-777"


def test_non_finalise_erreur():
    payload = dict(FIXTURE, state="STARTED")
    with pytest.raises(MeetRecordingError, match="finalisé"):
        MeetRecording.from_recording(payload)


def test_nom_invalide_erreur():
    payload = dict(FIXTURE, name="conferenceRecords/only")
    with pytest.raises(MeetRecordingError, match="nom"):
        MeetRecording.from_recording(payload)


def test_drive_file_manquant_erreur():
    payload = copy.deepcopy(FIXTURE)
    payload["driveDestination"] = {"exportUri": "https://x"}
    with pytest.raises(MeetRecordingError, match="driveDestination"):
        MeetRecording.from_recording(payload)


class TestAdapter:
    adapter = MeetRecordingAdapter()

    def test_occurrence_sur_conference_record(self):
        occ = self.adapter.to_occurrence(MeetRecording.from_recording(FIXTURE))
        assert occ.provider == "meet"
        assert occ.external_occurrence_id == "conf-abc"
        assert occ.provider_account_id == "spaces/space-777"

    def test_artifact_pointe_drive(self):
        art = self.adapter.to_artifact(MeetRecording.from_recording(FIXTURE))
        assert art.storage_uri == "gdrive://drive-file-999" and art.artifact_type == "recording"

    def test_dedup_composite(self):
        rec = MeetRecording.from_recording(FIXTURE)
        assert self.adapter.dedup_key(rec) == "meet|spaces/space-777|conf-abc|rec-xyz"

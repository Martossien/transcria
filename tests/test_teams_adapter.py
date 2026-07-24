"""A3 — adaptateur Teams Graph, contre une fixture de change notification réelle."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from connector_service.providers.teams import (
    TeamsNotificationError,
    TeamsRecording,
    TeamsRecordingAdapter,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "teams_recording_notification.json").read_text())


def test_parse_notification():
    rec = TeamsRecording.from_notification(FIXTURE)
    assert rec.tenant_meeting_id == "MSpORGmeeting"
    assert rec.recording_id == "REC-789" and rec.organizer_id == "org-aad-456"
    assert rec.change_type == "created"


def test_value_vide_erreur():
    with pytest.raises(TeamsNotificationError, match="value"):
        TeamsRecording.from_notification({"value": []})


def test_sans_ressource_recording_erreur():
    payload = copy.deepcopy(FIXTURE)
    payload["value"][0]["resourceData"] = {"@odata.type": "#microsoft.graph.chatMessage", "id": "x"}
    payload["value"][0]["resource"] = "chats('x')/messages('y')"
    with pytest.raises(TeamsNotificationError, match="callRecording"):
        TeamsRecording.from_notification(payload)


class TestAdapter:
    adapter = TeamsRecordingAdapter()

    def test_occurrence_sur_meeting_id(self):
        occ = self.adapter.to_occurrence(TeamsRecording.from_notification(FIXTURE))
        assert occ.provider == "teams"
        assert occ.external_occurrence_id == "MSpORGmeeting"
        assert occ.provider_account_id == "org-aad-456"

    def test_artifact_url_graph_content(self):
        art = self.adapter.to_artifact(TeamsRecording.from_notification(FIXTURE))
        assert art.storage_uri.startswith("https://graph.microsoft.com/v1.0/")
        assert art.storage_uri.endswith("/content") and art.artifact_type == "recording"

    def test_dedup_composite(self):
        rec = TeamsRecording.from_notification(FIXTURE)
        assert self.adapter.dedup_key(rec) == "teams|org-aad-456|MSpORGmeeting|REC-789"

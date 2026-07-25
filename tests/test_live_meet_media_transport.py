"""R1 — Meet Media API : requête/réponse connectActiveConference + attribution CSRC→locuteur."""
from __future__ import annotations

import pytest

from connector_service.live.meet_media_transport import (
    MediaEntriesRegistry,
    MeetMediaError,
    ParticipantsRegistry,
    connect_active_conference_request,
    connection_state,
    leave_request,
    meet_frame_from_rtp,
    parse_connect_response,
    pick_contributing_csrc,
)


def test_connect_request_forme():
    url, body, headers = connect_active_conference_request("spaces/abc", "v=0...", "GTOK")
    assert url == "https://meet.googleapis.com/v2beta/spaces/spaces/abc:connectActiveConference"
    assert body == {"offer": "v=0..."}                        # SDP = string sous `offer`
    assert headers["Authorization"] == "Bearer GTOK"


def test_parse_response_answer_et_erreur():
    assert parse_connect_response({"answer": "v=0 answer"}) == "v=0 answer"
    with pytest.raises(MeetMediaError, match="PERMISSION_DENIED"):
        parse_connect_response({"error": {"status": "PERMISSION_DENIED"}})
    with pytest.raises(MeetMediaError, match="UnknownError"):
        parse_connect_response({})


def test_media_entries_registry_csrc_et_suppression():
    reg = MediaEntriesRegistry()
    reg.apply({"resources": [
        {"id": 1, "mediaEntry": {"audioCsrc": 111, "participantKey": "participants/p1"}},
        {"id": 2, "mediaEntry": {"audioCsrc": 222, "participantKey": "participants/p2"}}]})
    assert reg.entry_for_csrc(111)["participantKey"] == "participants/p1"
    reg.apply({"deletedResources": [{"id": 1, "mediaEntry": True}]})
    assert reg.entry_for_csrc(111) is None and reg.entry_for_csrc(222) is not None


def test_participants_registry_display_name_trois_types():
    reg = ParticipantsRegistry()
    reg.apply({"resources": [
        {"id": 1, "participant": {"participantKey": "participants/p1",
                                  "signedInUser": {"displayName": "Alice"}}},
        {"id": 2, "participant": {"participantKey": "participants/p2",
                                  "anonymousUser": {"displayName": "Invité"}}}]})
    assert reg.display_name("participants/p1") == "Alice"
    assert reg.display_name("participants/p2") == "Invité"
    assert reg.display_name("participants/inconnu") == ""


def test_pick_contributing_csrc_saute_42():
    assert pick_contributing_csrc([42, 111, 222]) == 111       # saute le locuteur-le-plus-fort
    assert pick_contributing_csrc([42]) is None
    assert pick_contributing_csrc([]) is None


def test_meet_frame_attribue_le_participant():
    entries = MediaEntriesRegistry()
    entries.apply({"resources": [{"id": 1, "mediaEntry": {
        "audioCsrc": 111, "participantKey": "participants/p1"}}]})
    parts = ParticipantsRegistry()
    parts.apply({"resources": [{"id": 1, "participant": {
        "participantKey": "participants/p1", "signedInUser": {"displayName": "Alice"}}}]})

    known = meet_frame_from_rtp([42, 111], b"\x00\x00" * 480, entries, parts)
    assert known.participant_id == "participants/p1" and known.participant_name == "Alice"
    assert known.sample_rate_hz == 48000 and known.channels == 1

    unknown = meet_frame_from_rtp([999], b"\x00\x00" * 480, entries, parts)
    assert unknown.participant_id == "csrc-999" and unknown.participant_name == ""

    silent = meet_frame_from_rtp([42], b"\x00\x00" * 480, entries, parts)
    assert silent.participant_id == "unknown"                  # que le marqueur → non attribué


def test_session_control_state_et_leave():
    assert connection_state({"resources": [{"sessionStatus": {
        "connectionState": "STATE_JOINED"}}]}) == "STATE_JOINED"
    assert connection_state({"resources": []}) is None
    assert leave_request(7) == {"request": {"requestId": 7, "leave": {}}}

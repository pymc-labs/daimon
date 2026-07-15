"""Tests for build_create_routine_modal (routines_panel/views.py).

Covers the New Routine modal shape: callback_id, the four input blocks, agent
dropdown options, and private_metadata round-trip.
"""

from __future__ import annotations

from daimon.adapters.slack.agent_setup.state import decode_private_metadata
from daimon.adapters.slack.routines_panel.views import build_create_routine_modal


def test_build_create_routine_modal_has_callback_id_and_submit() -> None:
    view = build_create_routine_modal(team_id="T1", channel_id="C1", agent_names=["daimon"])

    assert view["type"] == "modal", "create modal must be type modal"
    assert view["callback_id"] == "routines__create", (
        "callback_id must be routines__create so app.py dispatch routes it"
    )
    assert "submit" in view, "create modal must have a submit button"
    assert len(view["title"]["text"]) <= 24, "modal title must be <= 24 chars (Slack cap)"


def test_build_create_routine_modal_has_four_input_blocks() -> None:
    view = build_create_routine_modal(team_id="T1", channel_id="C1", agent_names=["daimon"])

    block_ids = {b["block_id"] for b in view["blocks"]}
    assert block_ids == {
        "routines_create__agent",
        "routines_create__cron",
        "routines_create__timezone",
        "routines_create__message",
    }, "modal must carry the four expected input blocks"


def test_build_create_routine_modal_agent_options_from_names() -> None:
    view = build_create_routine_modal(
        team_id="T1", channel_id="C1", agent_names=["daimon", "research-bot"]
    )

    agent_block = next(b for b in view["blocks"] if b["block_id"] == "routines_create__agent")
    options = agent_block["element"]["options"]
    values = [o["value"] for o in options]
    assert values == ["daimon", "research-bot"], (
        "agent dropdown options must use the agent names as both text and value"
    )
    assert agent_block["element"]["action_id"] == "routines_create__agent", (
        "agent select action_id must be routines_create__agent"
    )


def test_build_create_routine_modal_timezone_initial_value_utc() -> None:
    view = build_create_routine_modal(team_id="T1", channel_id="C1", agent_names=["daimon"])

    tz_block = next(b for b in view["blocks"] if b["block_id"] == "routines_create__timezone")
    assert tz_block["element"]["initial_value"] == "UTC", "timezone field should default to UTC"


def test_build_create_routine_modal_message_is_multiline() -> None:
    view = build_create_routine_modal(team_id="T1", channel_id="C1", agent_names=["daimon"])

    msg_block = next(b for b in view["blocks"] if b["block_id"] == "routines_create__message")
    assert msg_block["element"]["multiline"] is True, "trigger message input must be multiline"


def test_build_create_routine_modal_private_metadata_round_trips() -> None:
    view = build_create_routine_modal(team_id="T_RT", channel_id="C_RT", agent_names=["daimon"])

    meta = decode_private_metadata(view["private_metadata"])
    assert meta.get("team_id") == "T_RT", "team_id must round-trip through private_metadata"
    assert meta.get("channel_id") == "C_RT", "channel_id must round-trip through private_metadata"

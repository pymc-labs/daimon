"""Tests for agent_setup/state.py.

Both metadata shapes: the flat dict the routines panel still writes, and the
typed `PanelMetadata` every agent-setup view carries. Round-trips, the
< 3000-char budget, and the total-function decoders.

No I/O, no DB, no mocks — pure unit assertions.
"""

import json

import pytest
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    decode_private_metadata,
    encode_panel_metadata,
    encode_private_metadata,
)
from daimon.adapters.slack.modal_limits import MAX_PRIVATE_METADATA_CHARS

# ---------------------------------------------------------------------------
# encode/decode round-trip — L1
# ---------------------------------------------------------------------------


def test_encode_decode_l1_round_trips_team_and_channel() -> None:
    encoded = encode_private_metadata(team_id="T01ABC123", channel_id="C01XYZ456")
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L1 round-trip should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L1 round-trip should preserve channel_id"


def test_encode_decode_l1_omits_none_fields() -> None:
    encoded = encode_private_metadata(team_id="T01ABC123", channel_id="C01XYZ456")
    decoded = decode_private_metadata(encoded)
    assert "selected_agent_name" not in decoded, (
        "L1 without selected_agent_name should not include that key"
    )
    assert "agent_name" not in decoded, "L1 without agent_name should not include that key"


def test_encode_decode_l1_includes_selected_agent_name_when_provided() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123", channel_id="C01XYZ456", selected_agent_name="my-agent"
    )
    decoded = decode_private_metadata(encoded)
    assert decoded.get("selected_agent_name") == "my-agent", (
        "L1 with selected_agent_name should round-trip the agent name"
    )


def test_encode_decode_l1_contains_no_workspace_derived_uuid() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123", channel_id="C01XYZ456", selected_agent_name="my-agent"
    )
    decoded = decode_private_metadata(encoded)
    assert "tenant_id" not in decoded, (
        "private_metadata must never contain tenant_id — it is always derived server-side"
    )


# ---------------------------------------------------------------------------
# encode/decode round-trip — L2
# ---------------------------------------------------------------------------


def test_encode_decode_l2_round_trips_all_identifiers() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        active_section="skills",
    )
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L2 should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L2 should preserve channel_id"
    assert decoded["agent_name"] == "my-agent", "L2 should preserve agent_name"
    assert decoded["active_section"] == "skills", "L2 should preserve active_section"


def test_encode_decode_l2_omits_l3_only_fields() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        active_section="agent",
    )
    decoded = decode_private_metadata(encoded)
    assert "parent_section" not in decoded, "L2 without parent_section should not include that key"


# ---------------------------------------------------------------------------
# encode/decode round-trip — L3
# ---------------------------------------------------------------------------


def test_encode_decode_l3_round_trips_all_identifiers() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        parent_section="mcps",
    )
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L3 should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L3 should preserve channel_id"
    assert decoded["agent_name"] == "my-agent", "L3 should preserve agent_name"
    assert decoded["parent_section"] == "mcps", "L3 should preserve parent_section"


# ---------------------------------------------------------------------------
# Character budget — must stay under 3,000 chars
# ---------------------------------------------------------------------------


def test_encode_private_metadata_worst_case_stays_under_3000_chars() -> None:
    """Worst-case: max-length Slack IDs + 64-char agent name."""
    # Slack workspace IDs are typically 9-11 chars (T01ABC123XYZ); Slack channel
    # IDs are similar length. Use a realistic worst case padded to be generous.
    long_agent_name = "a" * 64
    encoded = encode_private_metadata(
        team_id="T" + "0" * 10,
        channel_id="C" + "0" * 10,
        agent_name=long_agent_name,
        active_section="repo_auth",
        parent_section="secrets",
    )
    assert len(encoded) < 3000, "private_metadata must stay under the Slack 3000-char limit"


# ---------------------------------------------------------------------------
# decode — malformed / empty input
# ---------------------------------------------------------------------------


def test_decode_private_metadata_empty_string_returns_empty_dict() -> None:
    result = decode_private_metadata("")
    assert result == {}, "decode of empty string should return {} without raising"


def test_decode_private_metadata_malformed_json_returns_empty_dict() -> None:
    result = decode_private_metadata("{not valid json")
    assert result == {}, "decode of malformed JSON should return {} without raising"


def test_decode_private_metadata_partial_json_returns_empty_dict() -> None:
    result = decode_private_metadata('{"team_id": "T123"')  # missing closing brace
    assert result == {}, "decode of truncated JSON should return {} without raising"


# ---------------------------------------------------------------------------
# PanelMetadata — the read-only panel's private_metadata
# ---------------------------------------------------------------------------


def test_panel_metadata_round_trips_every_field() -> None:
    meta = PanelMetadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        view="details",
        page=3,
        agent_name="research-bot",
        root_view_id="V0123456789",
        expanded="connections",
    )
    decoded = decode_panel_metadata(encode_panel_metadata(meta))
    assert decoded == meta, "a fully populated panel metadata must survive the round trip"


def test_panel_metadata_omits_defaults_from_the_encoded_payload() -> None:
    meta = PanelMetadata(team_id="T01ABC123", channel_id="C01XYZ456", view="agents")
    encoded = encode_panel_metadata(meta)
    assert json.loads(encoded) == {"t": "T01ABC123", "c": "C01XYZ456", "v": "agents"}, (
        "a field still at its default costs no characters of the 3,000-character budget"
    )
    assert decode_panel_metadata(encoded) == meta, "the omitted fields come back as their defaults"


def test_panel_metadata_carries_no_tenant_or_agent_id() -> None:
    encoded = encode_panel_metadata(
        PanelMetadata(
            team_id="T01ABC123",
            channel_id="C01XYZ456",
            view="details",
            agent_name="research-bot",
        )
    )
    assert "tenant" not in encoded, "the tenant id is always re-derived server-side"
    assert "ma_agent_id" not in encoded, "an MA id is never taken from a client payload"


def test_panel_metadata_stays_well_inside_slacks_metadata_budget() -> None:
    encoded = encode_panel_metadata(
        PanelMetadata(
            team_id="T" * 32,
            channel_id="C" * 32,
            view="details",
            page=99,
            agent_name="n" * 64,
            root_view_id="V" * 32,
            expanded="connections",
        )
    )
    assert len(encoded) < MAX_PRIVATE_METADATA_CHARS, (
        "a worst-case panel payload must still fit Slack's private_metadata cap"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "[]",
        '{"c":"C1","v":"agents"}',
        '{"t":"T1","v":"agents"}',
        '{"t":"T1","c":"C1"}',
        '{"t":"T1","c":"C1","v":"nope"}',
        '{"t":"T1","c":"C1","v":"agents","p":-1}',
        '{"t":"T1","c":"C1","v":"agents","p":"two"}',
        '{"t":"T1","c":"C1","v":"agents","a":7}',
        '{"t":1,"c":"C1","v":"agents"}',
        '{"t":"T1","c":"C1","v":"agents","x":7}',
    ],
)
def test_decode_panel_metadata_when_malformed_returns_none(raw: str) -> None:
    assert decode_panel_metadata(raw) is None, (
        f"a payload the panel cannot trust must decode to None, not a half-read view ({raw!r})"
    )


def test_decode_panel_metadata_drops_unrecognised_expansions() -> None:
    decoded = decode_panel_metadata('{"t":"T1","c":"C1","v":"details","x":["keys","mystery"]}')
    assert decoded is not None, "a recognisable payload with one odd expansion still decodes"
    assert decoded.expanded == "keys", (
        "an expansion this build does not know about is ignored, not carried through"
    )


def test_with_page_moves_the_page_and_clamps_below_zero() -> None:
    meta = PanelMetadata(team_id="T1", channel_id="C1", view="agents", page=2)
    assert meta.with_page(5).page == 5, "with_page moves to the requested page"
    assert meta.with_page(-3).page == 0, "a page before the first clamps to the first"
    assert meta.page == 2, "with_page returns a new metadata rather than mutating"


def test_with_view_keeps_page() -> None:
    meta = PanelMetadata(team_id="T1", channel_id="C1", view="agents", page=4, root_view_id="V1")
    moved = meta.with_view("new_agent", root_view_id="V1")
    assert moved.view == "new_agent", "with_view switches screens"
    assert moved.root_view_id == "V1", "the root view id is carried when the caller passes it"
    assert moved.page == 4, (
        "the page travels so the root roster can be refreshed where the reader left it"
    )
    assert meta.view == "agents", "with_view returns a new metadata rather than mutating"


def test_with_view_when_page_given_starts_the_new_screen_there() -> None:
    meta = PanelMetadata(team_id="T1", channel_id="C1", view="agents", page=4)
    moved = meta.with_view("details", agent_name="research-bot", page=0)
    assert moved.agent_name == "research-bot", "the target agent travels with the view"
    assert moved.page == 0, "a screen that pages over its own list says so explicitly"
    assert meta.page == 4, "the view the reader came from is untouched, so Back restores it"
    assert meta.with_view("routing", page=-2).page == 0, "a page before the first clamps"


def test_with_view_resets_expansion_when_the_agent_changes() -> None:
    meta = PanelMetadata(
        team_id="T1",
        channel_id="C1",
        view="details",
        agent_name="researcher",
        expanded="keys",
    )
    assert meta.with_view("details", agent_name="researcher").expanded == "keys", (
        "an in-place rerender keeps the selected agent's open list"
    )
    assert meta.with_view("details", agent_name="forecaster").expanded is None, (
        "opening another agent starts with every list collapsed"
    )


def test_toggled_opens_a_collapsed_list_and_closes_an_open_one() -> None:
    meta = PanelMetadata(team_id="T1", channel_id="C1", view="details")
    opened = meta.toggled("keys")
    assert opened.expanded == "keys", "toggling a collapsed list opens it"
    assert opened.toggled("keys").expanded is None, "toggling it again closes it"
    skills = opened.toggled("skills")
    assert skills.expanded == "skills", "opening one list closes the previous list"
    assert meta.expanded is None, "toggled returns a new metadata rather than mutating"

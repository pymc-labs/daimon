"""Tests for the routing-truth notes shared by the MCP tool result and the
Discord setup panel confirmation.

Assert load-bearing substrings only (mention requirement, single-bot fact,
cleared vs. no-op), not exact sentences, so wording can improve without
rewriting these tests.
"""

from __future__ import annotations

from daimon.core.routing_facts import (
    UNROUTED_LINE,
    UNROUTED_NEXT_STEP,
    build_clear_default_note,
    build_routing_request,
    build_set_default_note,
    build_unrouted_note,
)


def test_build_set_default_note_is_pure() -> None:
    first = build_set_default_note(agent_name="writer", scope_label="workspace")
    second = build_set_default_note(agent_name="writer", scope_label="workspace")
    assert first == second, "build_set_default_note must be pure: same inputs, same output"


def test_build_clear_default_note_is_pure() -> None:
    first = build_clear_default_note(scope_label="workspace", cleared=True)
    second = build_clear_default_note(scope_label="workspace", cleared=True)
    assert first == second, "build_clear_default_note must be pure: same inputs, same output"


def test_build_set_default_note_states_mention_requirement() -> None:
    note = build_set_default_note(agent_name="writer", scope_label="workspace")
    assert "@mention" in note, "set-default note must state the mention requirement"


def test_build_set_default_note_states_single_bot_fact() -> None:
    note = build_set_default_note(agent_name="writer", scope_label="workspace")
    assert "one bot" in note.lower(), "set-default note must state the single-bot fact"


def test_build_set_default_note_includes_agent_name_and_scope_label() -> None:
    note = build_set_default_note(agent_name="writer", scope_label="channel:123")
    assert "writer" in note, "set-default note must name the agent that was set"
    assert "channel:123" in note, "set-default note must name the scope it applies to"


def test_build_set_default_note_reads_correctly_for_channel_scope() -> None:
    note = build_set_default_note(agent_name="writer", scope_label="channel:123")
    assert "@mention" in note and "one bot" in note.lower(), (
        "note must state both facts for a channel-scoped default"
    )


def test_build_clear_default_note_states_mention_requirement_when_cleared() -> None:
    note = build_clear_default_note(scope_label="workspace", cleared=True)
    assert "@mention" in note, "clear-default note must restate the mention requirement"


def test_build_clear_default_note_distinguishes_cleared_from_no_op() -> None:
    cleared_note = build_clear_default_note(scope_label="workspace", cleared=True)
    no_op_note = build_clear_default_note(scope_label="workspace", cleared=False)
    assert cleared_note != no_op_note, "cleared and no-op notes must read differently"
    assert "no" in no_op_note.lower() and "nothing changed" in no_op_note.lower(), (
        "no-op note must state that the scope had no default and nothing changed"
    )


def test_build_clear_default_note_includes_scope_label() -> None:
    note = build_clear_default_note(scope_label="channel:123", cleared=True)
    assert "channel:123" in note, "clear-default note must name the scope it applies to"


def test_build_routing_request_names_the_agent_and_the_channel() -> None:
    request = build_routing_request(agent_name="writer", channel_label="#general")
    assert request == "Make writer answer in #general.", (
        "the routing request is the sentence a person says to Daimon, verbatim"
    )


def test_build_unrouted_note_asks_for_the_known_channel_in_the_admins_own_voice() -> None:
    note = build_unrouted_note(agent_name="writer", channel_label="#general", is_admin=True)
    assert note.startswith(UNROUTED_LINE), "the note leads with the unrouted fact"
    assert "Tell Daimon: make writer answer in #general." in note, (
        "an admin who can act gets the request to make, in their own voice"
    )


def test_build_unrouted_note_routes_a_member_through_an_admin_for_the_known_channel() -> None:
    note = build_unrouted_note(agent_name="writer", channel_label="#general", is_admin=False)
    assert "An admin can tell Daimon: make writer answer in #general." in note, (
        "a member cannot route an agent, so the request is addressed to an admin"
    )


def test_build_unrouted_note_falls_back_to_the_generic_step_when_no_channel_is_known() -> None:
    admin = build_unrouted_note(agent_name="writer", channel_label=None, is_admin=True)
    member = build_unrouted_note(agent_name="writer", channel_label=None, is_admin=False)
    assert admin == f"{UNROUTED_LINE}\n{UNROUTED_NEXT_STEP}", (
        "with no channel to name there is no concrete request, only the generic next step"
    )
    assert admin == member, "the generic next step reads the same for both roles"
    assert "Tell Daimon" not in admin, (
        "with no channel to name, the note must not spell out a request to make"
    )

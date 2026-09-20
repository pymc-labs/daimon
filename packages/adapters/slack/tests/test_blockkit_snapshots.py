"""Syrupy snapshot tests for the Slack Block Kit render layer (SUITE-04).

Locks `to_blocks` output for a curated set of render-distinct `State`
instances, plus `build_error_view` for a fixed request_id. Determinism comes
from the required `now` kwarg (no wall-clock reads) and a fixed request_id
(no ULID nondeterminism).

To intentionally update a snapshot after a deliberate render change, run:
    uv run pytest packages/adapters/slack/tests/test_blockkit_snapshots.py --snapshot-update
then review the diff in `__snapshots__/` before committing.
"""

from __future__ import annotations

import pytest
from daimon.adapters.slack.agent_setup.panel_views import build_error_view
from daimon.adapters.slack.blockkit import State, TrailEntry, TurnPhase, to_blocks
from syrupy.assertion import SnapshotAssertion

_FIXED_REQUEST_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

_STATES: dict[str, tuple[State, float | None]] = {
    "minimal_thinking": (
        State(phase=TurnPhase.THINKING, agent_name="Atlas", started_at=100.0),
        None,
    ),
    "thinking_with_elapsed": (
        State(phase=TurnPhase.THINKING, agent_name="Atlas", started_at=100.0),
        110.0,
    ),
    "thinking_with_preview": (
        State(
            phase=TurnPhase.THINKING,
            agent_name="Atlas",
            started_at=100.0,
            text_preview="Let me check that for you.",
        ),
        110.0,
    ),
    "single_tool": (
        State(
            phase=TurnPhase.TOOL_RUNNING,
            agent_name="Atlas",
            started_at=100.0,
            trail=(TrailEntry(emoji="⚙️", text="Bash"),),
        ),
        110.0,
    ),
    "saturated_trail_with_preview_and_cost": (
        State(
            phase=TurnPhase.TOOL_RUNNING,
            agent_name="Atlas",
            started_at=100.0,
            trail=(
                TrailEntry(emoji="⚙️", text="Read"),
                TrailEntry(emoji="⚙️", text="Write"),
                TrailEntry(emoji="⚙️", text="Bash"),
                TrailEntry(emoji="⚙️", text="Grep"),
                TrailEntry(emoji="⚙️", text="Glob"),
            ),
            text_preview="Preview text describing the ongoing work.",
            usage_in=1500,
            usage_out=320,
            cost_str="$0.04",
        ),
        120.0,
    ),
    "terminal_done_with_cost": (
        State(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=100.0,
            trail=(TrailEntry(emoji="✅", text="complete"),),
            usage_in=1500,
            usage_out=320,
            cost_str="$0.04",
        ),
        142.0,
    ),
    "terminal_done_no_cost": (
        State(
            phase=TurnPhase.DONE,
            agent_name="Atlas",
            started_at=100.0,
            trail=(TrailEntry(emoji="✅", text="complete"),),
            usage_in=1500,
            usage_out=320,
        ),
        142.0,
    ),
    "terminal_error_with_reason": (
        State(
            phase=TurnPhase.ERROR,
            agent_name="Atlas",
            started_at=100.0,
            trail=(TrailEntry(emoji="❌", text="rate limited"),),
            usage_in=100,
            usage_out=50,
        ),
        112.0,
    ),
}


@pytest.mark.parametrize("name", list(_STATES), ids=list(_STATES))
def test_to_blocks_snapshot(name: str, snapshot: SnapshotAssertion) -> None:
    state, now = _STATES[name]
    assert to_blocks(state, now=now) == snapshot


def test_build_error_view_snapshot(snapshot: SnapshotAssertion) -> None:
    assert build_error_view(request_id=_FIXED_REQUEST_ID) == snapshot

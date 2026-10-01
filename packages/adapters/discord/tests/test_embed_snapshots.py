"""Syrupy snapshot tests for the Discord embed render layer (SUITE-04, D-09/D-10).

Locks `to_embed_data` output for a curated set of
render-distinct `EmbedState` instances, plus `render_error` for a fixed
request_id. Determinism comes from the injectable `now` kwarg (no wall-clock
reads) and a fixed request_id (no ULID nondeterminism).

To intentionally update a snapshot after a deliberate render change, run:
    uv run pytest packages/adapters/discord/tests/test_embed_snapshots.py --snapshot-update
then review the diff in `__snapshots__/` before committing.
"""

from __future__ import annotations

import pytest
from daimon.adapters.discord.embed import (
    EmbedData,
    EmbedState,
    TurnPhase,
    to_embed_data,
)
from daimon.adapters.discord.errors import render_error
from daimon.core.errors import SpecError
from syrupy.assertion import SnapshotAssertion

_FIXED_NOW = 142.0
_FIXED_REQUEST_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _render(state: EmbedState) -> EmbedData:
    return to_embed_data(state, now=_FIXED_NOW)


_STATES: dict[str, EmbedState] = {
    "minimal_thinking": EmbedState(phase=TurnPhase.THINKING, agent_name="Atlas", started_at=100.0),
    "thinking_no_now": EmbedState(phase=TurnPhase.THINKING, agent_name="Atlas", started_at=0.0),
    "preview_present": EmbedState(
        phase=TurnPhase.THINKING,
        agent_name="Atlas",
        started_at=100.0,
        text_preview="Let me check that for you.",
    ),
    "single_tool": EmbedState(
        phase=TurnPhase.TOOL_RUNNING,
        agent_name="Atlas",
        started_at=100.0,
        tool_lines=("🖋️ Running a command",),
    ),
    "saturated_trail_with_preview_and_cost": EmbedState(
        phase=TurnPhase.TOOL_RUNNING,
        agent_name="Atlas",
        started_at=100.0,
        tool_lines=(
            "+3 earlier",
            "✔️ Read a file",
            "🚫 Wrote a file",
            "✔️ Search issues (tracker)",
            "✔️ Listed files",
            "🔍 Searching files",
            "🖋️ Running a command",
        ),
        text_preview="Preview text describing the ongoing work.",
        usage_in=1500,
        usage_out=320,
        cost_str="$0.04",
    ),
    "terminal_done_no_cost": EmbedState(
        phase=TurnPhase.DONE,
        agent_name="Atlas",
        started_at=100.0,
        usage_in=1500,
        usage_out=320,
    ),
    "terminal_done_with_cost": EmbedState(
        phase=TurnPhase.DONE,
        agent_name="Atlas",
        started_at=100.0,
        usage_in=1500,
        usage_out=320,
        cost_str="$0.04",
    ),
    "terminal_error_with_reason": EmbedState(
        phase=TurnPhase.ERROR,
        agent_name="Atlas",
        started_at=100.0,
        error_reason="rate limited",
        usage_in=100,
        usage_out=50,
    ),
    "terminal_error_no_trail": EmbedState(
        phase=TurnPhase.ERROR, agent_name="Atlas", started_at=100.0
    ),
}


@pytest.mark.parametrize("name", list(_STATES), ids=list(_STATES))
def test_embed_render_snapshot(name: str, snapshot: SnapshotAssertion) -> None:
    assert _render(_STATES[name]) == snapshot


class TestRenderErrorSnapshot:
    def test_spec_error_snapshot(self, snapshot: SnapshotAssertion) -> None:
        exc = SpecError("agent 'atlas' is not configured for this tenant")
        assert render_error(exc, request_id=_FIXED_REQUEST_ID) == snapshot

    def test_generic_exception_snapshot(self, snapshot: SnapshotAssertion) -> None:
        exc = ValueError("bad input")
        assert render_error(exc, request_id=_FIXED_REQUEST_ID) == snapshot

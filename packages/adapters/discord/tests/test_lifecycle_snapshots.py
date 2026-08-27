"""Syrupy snapshot tests for DiscordTurnLifecycle payloads (SUITE-04, D-09/D-10).

Drives a representative SSE sequence through a real DiscordTurnLifecycle with
an injected fixed clock (05-01), captures the send/edit payloads recorder-style
(same pattern as test_lifecycle.py), and snapshots the resulting embed data.
No time.monotonic() reads reach the lifecycle, so payloads are stable across
runs.

To intentionally update a snapshot after a deliberate render change, run:
    uv run pytest packages/adapters/discord/tests/test_lifecycle_snapshots.py --snapshot-update
then review the diff in `__snapshots__/` before committing.
"""

from __future__ import annotations

import types
from typing import Any

from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.turn.state import TextBlock, TurnState, UsageTotals
from syrupy.assertion import SnapshotAssertion

_SENTINEL_REF = object()


def _thinking_event() -> Any:
    return types.SimpleNamespace(type="agent.thinking")


def _tool_use_event(name: str) -> Any:
    return types.SimpleNamespace(type="agent.tool_use", name=name)


def _make_clock(times: list[float]) -> Any:
    """Return a clock callable that yields `times` in order, repeating the last
    value once exhausted (extra internal reads stay deterministic)."""
    remaining = list(times)

    def clock() -> float:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return clock


def _make_lifecycle(
    clock: Any,
) -> tuple[DiscordTurnLifecycle, list[dict[str, Any]], list[tuple[Any, dict[str, Any]]]]:
    sends: list[dict[str, Any]] = []
    edits: list[tuple[Any, dict[str, Any]]] = []

    async def fake_send(**kwargs: Any) -> object:
        sends.append(kwargs)
        return _SENTINEL_REF

    async def fake_edit(ref: Any, **kwargs: Any) -> None:
        edits.append((ref, kwargs))

    lc = DiscordTurnLifecycle(
        send=fake_send,
        edit=fake_edit,
        agent_name="Atlas",
        model_id="claude-sonnet-4-6",
        clock=clock,
    )
    return lc, sends, edits


def _embed_snapshot(embed: Any) -> dict[str, Any]:
    """Reduce a discord.Embed to a plain, snapshot-friendly dict."""
    return {
        "title": embed.title,
        "description": embed.description,
        "color": embed.colour.value if embed.colour is not None else None,
        "footer": embed.footer.text if embed.footer else None,
    }


def _sends_snapshot(sends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "content": s.get("content"),
            "embeds": [_embed_snapshot(e) for e in s.get("embeds", [])],
        }
        for s in sends
    ]


def _edits_snapshot(edits: list[tuple[Any, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "content": kwargs.get("content"),
            "embeds": [_embed_snapshot(e) for e in kwargs.get("embeds", [])] or None,
        }
        for _, kwargs in edits
    ]


class TestSuccessSequenceSnapshot:
    async def test_thinking_tool_then_done_sequence(self, snapshot: SnapshotAssertion) -> None:
        clock = _make_clock([100.0, 100.0, 111.0, 111.0, 130.0, 130.0])
        lc, sends, edits = _make_lifecycle(clock)

        # D-11: on_sse_event no longer flushes -- the render tick does. Each
        # event is followed by the render call production drives it with.
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())
        await lc.on_sse_event(_tool_use_event("Bash"))
        await lc.on_render(TurnState())

        state = TurnState(
            content=[TextBlock(kind="text", text="Here is the answer.")],
            usage_totals=UsageTotals(
                input_tokens=1200,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=300,
                output_tokens=320,
            ),
        )
        await lc.on_terminal_success(state)

        assert {"sends": _sends_snapshot(sends), "edits": _edits_snapshot(edits)} == snapshot


class TestFailureSnapshot:
    async def test_terminal_failure_sequence(self, snapshot: SnapshotAssertion) -> None:
        clock = _make_clock([100.0, 100.0, 115.0, 115.0])
        lc, sends, edits = _make_lifecycle(clock)

        # D-11: on_sse_event no longer flushes -- the render tick does.
        await lc.on_sse_event(_thinking_event())
        await lc.on_render(TurnState())

        state = TurnState(
            usage_totals=UsageTotals(
                input_tokens=500,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=80,
            ),
        )
        await lc.on_terminal_failure(state, Exception("upstream timeout"))

        assert {"sends": _sends_snapshot(sends), "edits": _edits_snapshot(edits)} == snapshot

"""Syrupy snapshot tests for SlackTurnLifecycle payloads (SUITE-04).

Drives a representative SSE sequence through a real SlackTurnLifecycle with
an injected fixed clock (05-01) against the transport-level
`fake_slack_web_client` fixture, then snapshots the blocks/text captured from
the chat.postMessage / chat.update requests. No time.monotonic() reads reach
the lifecycle, so payloads are stable across runs.

To intentionally update a snapshot after a deliberate render change, run:
    uv run pytest packages/adapters/slack/tests/test_lifecycle_snapshots.py --snapshot-update
then review the diff in `__snapshots__/` before committing.
"""

from __future__ import annotations

import asyncio
import copy
import types
from typing import Any

import yarl
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.core.turn.state import TextBlock, TurnState, UsageTotals
from syrupy.assertion import SnapshotAssertion

_POST_URL = yarl.URL("https://slack.com/api/chat.postMessage")
_UPDATE_URL = yarl.URL("https://slack.com/api/chat.update")


def _make_clock(times: list[float]) -> Any:
    """Return a clock callable that yields `times` in order, repeating the last
    value once exhausted (extra internal reads stay deterministic)."""
    remaining = list(times)

    def clock() -> float:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return clock


def _thinking_event() -> Any:
    return types.SimpleNamespace(type="agent.thinking")


def _tool_use_event(name: str) -> Any:
    return types.SimpleNamespace(type="agent.tool_use", name=name)


def _make_lifecycle(fake: Any, clock: Any) -> SlackTurnLifecycle:
    return SlackTurnLifecycle(
        client=fake.client,
        channel="C_TEST",
        thread_ts="1700000000.000000",
        cancel=asyncio.Event(),
        author_id="U_AUTHOR",
        agent_name="Atlas",
        model_id="claude-sonnet-4-6",
        register=lambda ts, ev, author_id: None,
        deregister=lambda ts: None,
        clock=clock,
    )


def _calls_snapshot(fake: Any, url: yarl.URL) -> list[dict[str, Any]]:
    calls = fake.mock.requests.get(("POST", url), [])
    snapshots: list[dict[str, Any]] = []
    for call in calls:
        body: dict[str, Any] = call.kwargs["json"]
        blocks = copy.deepcopy(body.get("blocks"))
        for block in blocks or []:
            if block.get("type") != "actions":
                continue
            for element in block.get("elements", []):
                if element.get("action_id") == "cancel_turn" and "value" in element:
                    element["value"] = "<turn-key>"
        snapshots.append({"blocks": blocks, "text": body.get("text")})
    return snapshots


async def test_thinking_tool_then_success_sequence(
    fake_slack_web_client: Any, snapshot: SnapshotAssertion
) -> None:
    clock = _make_clock([100.0, 100.0, 106.0, 116.0])
    lc = _make_lifecycle(fake_slack_web_client, clock)

    await lc.post_initial()
    await lc.on_sse_event(_thinking_event())
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

    assert {
        "posts": _calls_snapshot(fake_slack_web_client, _POST_URL),
        "updates": _calls_snapshot(fake_slack_web_client, _UPDATE_URL),
    } == snapshot


async def test_terminal_failure_sequence(
    fake_slack_web_client: Any, snapshot: SnapshotAssertion
) -> None:
    clock = _make_clock([100.0, 100.0, 112.0])
    lc = _make_lifecycle(fake_slack_web_client, clock)

    await lc.post_initial()
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

    assert {
        "posts": _calls_snapshot(fake_slack_web_client, _POST_URL),
        "updates": _calls_snapshot(fake_slack_web_client, _UPDATE_URL),
    } == snapshot

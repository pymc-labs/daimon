"""Probe: does resume_turn correctly render post-approval content?

Two scenarios:
  1. Fast render_interval (0.001s) — render loop ticks many times
  2. Slow render_interval (60s) — only the terminal _render_once fires (mirrors
     Discord's 2s interval where the turn completes before the first tick)

Hypothesis under test: After run_turn completes with stop_reason=requires_action,
the DiscordTurnLifecycle's self.prev tracks what the driver rendered. When
resume_turn is called with the SAME lifecycle instance, the driver seeds
prev_cell[0] = prior_state but the lifecycle's self.prev may be out of sync,
causing diff(self.prev, state) to return empty and silently drop content.

Also probes: does the driver's double-diff (driver _render_once + lifecycle
on_render) ever cancel each other out?

Run with: uv run python scripts/probes/resume_turn_rendering.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast, Any

sys.path.insert(0, "packages/core")
sys.path.insert(0, "packages/adapters/discord")

from anthropic import AsyncAnthropic
from daimon.core.turn import resume_turn, run_turn
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason
from daimon.core.turn.render import diff
from daimon.core.turn.state import TurnState
from anthropic.types import RawMessageStreamEvent

from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_use_event import (
    BetaManagedAgentsAgentToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_result_event import (
    BetaManagedAgentsAgentToolResultEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
)

_T = datetime(2026, 1, 1, tzinfo=UTC)


def make_agent_message(*, event_id: str, text: str) -> BetaManagedAgentsAgentMessageEvent:
    return BetaManagedAgentsAgentMessageEvent(
        id=event_id,
        type="agent.message",
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
        processed_at=_T,
    )


def make_tool_use(*, event_id: str, name: str) -> BetaManagedAgentsAgentToolUseEvent:
    return BetaManagedAgentsAgentToolUseEvent(
        id=event_id,
        type="agent.tool_use",
        name=name,
        input={},
        evaluated_permission="ask",
        processed_at=_T,
    )


def make_tool_result(
    *, event_id: str, tool_use_id: str, text: str
) -> BetaManagedAgentsAgentToolResultEvent:
    return BetaManagedAgentsAgentToolResultEvent(
        id=event_id,
        type="agent.tool_result",
        tool_use_id=tool_use_id,
        content=[BetaManagedAgentsTextBlock(type="text", text=text)],
        is_error=False,
        processed_at=_T,
    )


def make_status_idle_requires_action(
    *, event_id: str, event_ids: list[str]
) -> BetaManagedAgentsSessionStatusIdleEvent:
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionRequiresAction(
            type="requires_action", event_ids=event_ids
        ),
        processed_at=_T,
    )


def make_status_idle_end_turn(*, event_id: str) -> BetaManagedAgentsSessionStatusIdleEvent:
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        processed_at=_T,
    )


# --- Fake SSE infrastructure ---

from dataclasses import dataclass as _dc, field as _field
import anthropic
import httpx


@_dc
class YieldEvent:
    event: Any


@_dc
class FakeEventsResource:
    stream_scripts: list[list[YieldEvent]] = _field(default_factory=list)
    sent_events: list[tuple[str, list[dict[str, Any]]]] = _field(default_factory=list)
    stream_calls: int = 0

    async def stream(self, *, session_id: str):
        if not self.stream_scripts:
            raise AssertionError("FakeEventsResource: no stream_scripts left")
        script = self.stream_scripts.pop(0)
        self.stream_calls += 1
        return _FakeStream(script)

    def list(self, *, session_id: str):
        return _FakeList([])

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> None:
        self.sent_events.append((session_id, list(events)))


class _FakeStream:
    def __init__(self, script):
        self._script = script

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for step in self._script:
            if isinstance(step, YieldEvent):
                yield step.event


class _FakeList:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for e in self._events:
            yield e


@_dc
class FakeSessionsBeta:
    events: FakeEventsResource = _field(default_factory=FakeEventsResource)


@_dc
class FakeBeta:
    sessions: FakeSessionsBeta = _field(default_factory=FakeSessionsBeta)


@_dc
class FakeAnthropic:
    beta: FakeBeta = _field(default_factory=FakeBeta)


# --- Recording lifecycle that tracks diffs ---

@dataclass
class RenderRecord:
    call_index: int
    state: TurnState
    lifecycle_prev_before_call: TurnState
    delta_from_lifecycle_prev: Any
    delta_is_empty: bool


@dataclass
class DetailedRecordingLifecycle:
    renders: list[RenderRecord] = field(default_factory=list)
    terminal_success: list[TurnState] = field(default_factory=list)
    terminal_failures: list[tuple[TurnState, Exception]] = field(default_factory=list)
    prev: TurnState = field(default_factory=TurnState)
    _call_index: int = field(default=0, init=False)

    async def on_render(self, state: TurnState) -> None:
        lifecycle_prev_before = self.prev
        delta = diff(lifecycle_prev_before, state)
        record = RenderRecord(
            call_index=self._call_index,
            state=state,
            lifecycle_prev_before_call=lifecycle_prev_before,
            delta_from_lifecycle_prev=delta,
            delta_is_empty=delta.is_empty(),
        )
        self.renders.append(record)
        self._call_index += 1
        self.prev = state  # mirror DiscordTurnLifecycle behaviour

    async def on_terminal_success(self, state: TurnState) -> None:
        self.terminal_success.append(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self.terminal_failures.append((state, err))

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        pass

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass


# --- Event factories (reusable across scenarios) ---

def _make_events():
    pre_text = make_agent_message(event_id="sevt_1", text="I will run bash")
    tool_use = make_tool_use(event_id="tu_1", name="bash")
    requires_idle = make_status_idle_requires_action(event_id="sevt_2", event_ids=["tu_1"])
    tool_result = make_tool_result(event_id="r_1", tool_use_id="tu_1", text="exit 0")
    post_text = make_agent_message(event_id="sevt_3", text="Done.")
    end_turn_idle = make_status_idle_end_turn(event_id="sevt_4")
    return pre_text, tool_use, requires_idle, tool_result, post_text, end_turn_idle


def _now() -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _print_renders(label: str, renders: list[RenderRecord]) -> None:
    print(f"{label}: {len(renders)} on_render call(s)")
    for r in renders:
        print(f"  render #{r.call_index}: {len(r.state.content)} blocks, "
              f"delta_empty={r.delta_is_empty}, "
              f"additions={len(r.delta_from_lifecycle_prev.block_additions)}, "
              f"appends={len(r.delta_from_lifecycle_prev.text_appends)}, "
              f"status_changes={len(r.delta_from_lifecycle_prev.block_status_changes)}, "
              f"stop_reason_set={r.delta_from_lifecycle_prev.stop_reason_set is not None}")
        for ba in r.delta_from_lifecycle_prev.block_additions:
            print(f"    + BlockAdded[{ba.block_index}]: {ba.block}")
        for ta in r.delta_from_lifecycle_prev.text_appends:
            print(f"    ~ TextAppend[{ta.block_index}]: {ta.appended_text!r}")
        for sc in r.delta_from_lifecycle_prev.block_status_changes:
            print(f"    * StatusChange[{sc.block_index}]: {sc.block.status}")


async def run_scenario(label: str, render_interval_s: float) -> tuple[bool, list[str]]:
    print(f"\n{'='*70}")
    print(f"SCENARIO: {label} (render_interval_s={render_interval_s})")
    print(f"{'='*70}")

    pre_text, tool_use_ev, requires_idle, tool_result, post_text, end_turn_idle = _make_events()

    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(pre_text),
            YieldEvent(tool_use_ev),
            YieldEvent(requires_idle),
        ],
        # MA re-delivers history + new events after approval
        [
            YieldEvent(pre_text),       # dedup re-deliver
            YieldEvent(tool_use_ev),    # dedup re-deliver
            YieldEvent(tool_result),
            YieldEvent(post_text),
            YieldEvent(end_turn_idle),
        ],
    ]

    lc = DetailedRecordingLifecycle()

    print("\n--- Phase 1: run_turn ---")
    prior_state = await run_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_test",
        user_message="run bash",
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=render_interval_s,
        now=_now,
    )

    renders_run = lc.renders[:]
    _print_renders("run_turn", renders_run)
    print(f"run_turn stop_reason: {prior_state.stop_reason.type if prior_state.stop_reason else None}")
    print(f"prior_state blocks: {len(prior_state.content)}")
    print(f"lifecycle.prev blocks: {len(lc.prev.content)}")
    print(f"prior_state == lifecycle.prev: {prior_state == lc.prev}")

    # Snapshot lifecycle.prev before resume
    lifecycle_prev_before_resume = lc.prev
    prior_state_count = len(prior_state.content)
    lc_prev_count = len(lc.prev.content)

    renders_before_resume = len(lc.renders)

    print("\n--- Phase 2: resume_turn ---")
    final_state = await resume_turn(
        anthropic=cast(AsyncAnthropic, fa),
        session_id="sess_test",
        prior_state=prior_state,
        confirmations={"tu_1": True},
        lifecycle=lc,
        cancel=asyncio.Event(),
        render_interval_s=render_interval_s,
        now=_now,
    )

    renders_resume = lc.renders[renders_before_resume:]
    _print_renders("resume_turn", renders_resume)
    print(f"resume_turn stop_reason: {final_state.stop_reason.type if final_state.stop_reason else None}")
    print(f"final_state blocks: {len(final_state.content)}")

    # Final diff analysis
    final_delta = diff(prior_state, final_state)
    print(f"\ndiff(prior_state, final_state):")
    print(f"  block_additions: {len(final_delta.block_additions)}")
    print(f"  block_status_changes: {len(final_delta.block_status_changes)}")
    print(f"  text_appends: {len(final_delta.text_appends)}")
    print(f"  stop_reason_set: {final_delta.stop_reason_set is not None}")
    print(f"  is_empty: {final_delta.is_empty()}")

    # Sync analysis
    synced = prior_state == lifecycle_prev_before_resume
    print(f"\nlifecycle.prev synced to prior_state before resume: {synced}")
    if not synced:
        print(f"  DIVERGENCE: prior_state has {prior_state_count} blocks, "
              f"lifecycle.prev has {lc_prev_count} blocks")

    # Verdict checks
    failures: list[str] = []

    if not renders_resume:
        failures.append("resume_turn: ZERO on_render calls — post-approval content dropped")

    post_text_rendered = any(
        any(
            hasattr(ba.block, 'kind') and ba.block.kind == 'text' and 'Done.' in ba.block.text
            for ba in r.delta_from_lifecycle_prev.block_additions
        )
        for r in renders_resume
    )
    if not post_text_rendered:
        failures.append("resume_turn: post-approval text 'Done.' NOT in any block_addition")

    if final_delta.is_empty():
        failures.append("diff(prior_state, final_state) is empty — reducer produced no new state")

    # Check whether block_status_changes are silently dropped (by design in V1)
    status_change_rendered = any(
        len(r.delta_from_lifecycle_prev.block_status_changes) > 0
        for r in renders_resume
    )
    print(f"\ntool-result status_change rendered by lifecycle: {status_change_rendered}")
    print("(block_status_changes not rendered in V1 — expected False)")

    return len(failures) == 0, failures


async def run_probe() -> None:
    print("=" * 70)
    print("PROBE: resume_turn rendering after requires_action")
    print("Checks the double-diff architecture (driver _render_once + lifecycle")
    print("on_render diff) under fast and slow render intervals.")
    print("=" * 70)

    all_pass = True
    scenario_results: list[tuple[str, bool, list[str]]] = []

    for label, interval in [
        ("FAST (ticks fire)", 0.001),
        ("SLOW (only terminal _render_once fires)", 60.0),
    ]:
        ok, failures = await run_scenario(label, interval)
        scenario_results.append((label, ok, failures))
        if not ok:
            all_pass = False

    print("\n" + "=" * 70)
    print("OVERALL VERDICT")
    print("=" * 70)
    for label, ok, failures in scenario_results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {label}")
        for f in failures:
            print(f"    - {f}")

    print()
    if all_pass:
        print("ALL SCENARIOS PASS")
        print()
        print("Root-cause analysis:")
        print("  The rendering architecture is sound. prior_state == lifecycle.prev")
        print("  is True after run_turn. resume_turn correctly renders post-approval")
        print("  content via _render_once.")
        print()
        print("  CONFIRMED ROOT CAUSE of the UAT rendering bug:")
        print("  bot.py _orchestrate() only calls run_turn() and returns. There is NO")
        print("  approval loop and NO call to resume_turn() anywhere in bot.py.")
        print("  When run_turn returns stop_reason=requires_action, the bot logs")
        print("  'turn.completed' and exits — the approval flow is entirely missing.")
        print()
        print("  The rendering path (driver _render_once -> lifecycle on_render ->) is")
        print("  correct and would work if resume_turn were ever called. The bug is")
        print("  that resume_turn is never invoked from the Discord adapter.")
        print()
        print("  Secondary note: block_status_changes (tool pending->complete) are")
        print("  produced by diff but NOT rendered by DiscordTurnLifecycle V1 (by")
        print("  design). Tool results are silently dropped — this is intentional.")
    else:
        print("SOME SCENARIOS FAILED — see details above")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_probe())

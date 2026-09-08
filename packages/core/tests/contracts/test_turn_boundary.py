"""Contract tests pinning the live MA facts the turn boundary is built on:

(a) a bare `idle` status can be observed immediately after a send — status
    alone is never a valid completion signal;
(b) the send response's echoed event carries no `processed_at` at all, and
    a `created_at_gte` filter anchored on the caller's OWN pre-send clock
    reading (not the echo) includes the boundary event and excludes every
    event from an earlier turn on the same session;
(c)-(e) `user.interrupt` converges to a terminal state from `running`, from
    `rescheduling` (skipped with a reason if that state cannot be reached
    deterministically), and is a no-op when sent to an already-idle session.

(b) supersedes an earlier version of this suite that expected the echo to
carry a usable `processed_at`. A live run showed the echo's `processed_at`
is always None; the timestamp only appears roughly half a second later,
once the agent starts on the event. The boundary's clock has to be the
caller's own, captured immediately before the send.

Runtime discipline: haiku only, one session per test. Env-gated by
DAIMON_TEST_ANTHROPIC_API_KEY; the default `uv run pytest` deselects the
contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaCloudConfigParams, BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.sessions.beta_managed_agents_user_interrupt_event_params import (
    BetaManagedAgentsUserInterruptEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)

pytestmark = pytest.mark.contract

_POLL_INTERVAL_S = 0.5
_RUNNING_WAIT_S = 15.0
_RESCHEDULING_WAIT_S = 10.0
_TERMINAL_WAIT_S = 30.0
_IDLE_WAIT_S = 15.0

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped real environment. Cleaned up by conftest's workspace wipe."""
    name = f"contract-test-boundary-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(name=name, config=_ENV_CONFIG)


@pytest_asyncio.fixture(scope="module")
async def live_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped real agent with no tools — status/boundary tests need only a reply."""
    name = f"contract-test-boundary-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract turn-boundary test"
    )


@pytest_asyncio.fixture(scope="module")
async def live_bash_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped real agent with a bash tool — interrupt tests need a
    command that stays running long enough to observe."""
    name = f"contract-test-boundary-bash-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name,
        model={"id": "claude-haiku-4-5"},
        system="You are a test agent. Run exactly the bash commands you are given.",
        tools=[{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
    )


async def _poll_until(
    anthropic_client: AsyncAnthropic,
    session_id: str,
    predicate: Callable[[str], bool],
    deadline_s: float,
) -> str | None:
    """Poll `sessions.retrieve` every `_POLL_INTERVAL_S` until `predicate` is
    true or `deadline_s` elapses. Returns the matching status, or None."""
    deadline = asyncio.get_running_loop().time() + deadline_s
    while True:
        session = await anthropic_client.beta.sessions.retrieve(session_id)
        if predicate(session.status):
            return session.status
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _wait_for_idle_event_since(
    anthropic_client: AsyncAnthropic,
    session_id: str,
    since: dt.datetime,
    deadline_s: float,
) -> str | None:
    """Poll `events.list` for a `session.status_idle` event at or after `since`.

    A session's status can still read `idle` for a brief window right after a
    send — the flip to `running` lags by roughly half a second — so polling
    `sessions.retrieve` immediately after a send can observe a turn that has
    not actually started yet. An idle EVENT that lands after the boundary,
    not a status snapshot, is the only completion signal this suite trusts.
    Returns the first matching event's id, or None if none appears within
    the deadline.
    """
    deadline = asyncio.get_running_loop().time() + deadline_s
    while True:
        async for event in anthropic_client.beta.sessions.events.list(
            session_id=session_id,
            created_at_gte=since,
            types=["session.status_idle"],
            order="asc",
        ):
            return event.id
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _interrupt_and_wait_for_terminal(
    anthropic_client: AsyncAnthropic, session_id: str, deadline_s: float
) -> str:
    """Send `user.interrupt`, then poll until a `session.status_idle` event
    is observable (session status is `idle` AND the event is listed) or the
    session reaches `terminated`. Returns which terminal signal was seen.

    Raises AssertionError with the last observed status on timeout — this is
    itself the assertion the interrupt-convergence tests make.
    """
    interrupt: BetaManagedAgentsUserInterruptEventParams = {"type": "user.interrupt"}
    await anthropic_client.beta.sessions.events.send(session_id, events=[interrupt])

    deadline = asyncio.get_running_loop().time() + deadline_s
    last_status = "unknown"
    while asyncio.get_running_loop().time() < deadline:
        session = await anthropic_client.beta.sessions.retrieve(session_id)
        last_status = session.status
        if last_status == "terminated":
            return "terminated"
        if last_status == "idle":
            async for event in anthropic_client.beta.sessions.events.list(
                session_id=session_id, types=["session.status_idle"], order="desc", limit=1
            ):
                assert event.type == "session.status_idle"
                return "session.status_idle"
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"interrupt did not converge to a terminal state within {deadline_s}s "
        f"(last observed status: {last_status!r})"
    )


async def test_status_immediately_after_send_is_not_a_completion_signal(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id, environment_id=live_environment.id
    )
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: DONE"}],
    }
    await anthropic_client.beta.sessions.events.send(session.id, events=[message])
    retrieved = await anthropic_client.beta.sessions.retrieve(session.id)
    assert retrieved.status in ("idle", "running", "rescheduling"), (
        f"status immediately after a send must be idle, running or rescheduling; "
        f"observed {retrieved.status!r}. A bare idle here being possible is exactly "
        "why status alone is never trusted as a completion signal."
    )


async def test_send_echo_carries_no_processed_at(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    """The fact D-07 r4 rests on: the send response's echoed event has no
    processed_at at all, immediately after the send returns. A timestamp
    only appears roughly half a second later, once the agent starts on the
    event — too late to serve as the boundary's clock, which is why the
    seam captures its own reading before the send instead."""
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id, environment_id=live_environment.id
    )
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: DONE"}],
    }
    sent = await anthropic_client.beta.sessions.events.send(session.id, events=[message])
    assert sent.data, "send must echo back the sent event"
    boundary_event = sent.data[0]
    assert boundary_event.processed_at is None, (
        f"the send echo is expected to carry no processed_at immediately after the "
        f"send returns; observed {boundary_event.processed_at!r}. If this ever starts "
        "returning a timestamp, the boundary's clock source should be reconsidered."
    )


async def test_created_at_gte_with_a_pre_send_clock_bound_isolates_the_second_turn(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    """A created_at_gte filter anchored on the CALLER's own pre-send clock
    reading (never the echo, which carries none) includes the second turn's
    boundary event and excludes every event from the first turn on the same
    session. Also proves that filtering by type still surfaces the boundary
    event first."""
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id, environment_id=live_environment.id
    )
    first_turn_started_at = dt.datetime.now(dt.UTC)
    first_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: FIRST"}],
    }
    await anthropic_client.beta.sessions.events.send(session.id, events=[first_message])
    first_idle_event_id = await _wait_for_idle_event_since(
        anthropic_client, session.id, first_turn_started_at, _IDLE_WAIT_S
    )
    assert first_idle_event_id is not None, (
        f"the first turn's session.status_idle event never appeared within {_IDLE_WAIT_S}s "
        "— the second turn's boundary cannot be exercised without a finished first turn to "
        "isolate from"
    )
    first_turn_events = [
        event
        async for event in anthropic_client.beta.sessions.events.list(
            session_id=session.id, order="asc"
        )
    ]
    first_turn_ids = {event.id for event in first_turn_events}
    assert first_turn_ids, "the first turn must have produced events before it can be isolated from"
    assert any(event.type == "agent.message" for event in first_turn_events), (
        "the first turn must have produced an agent.message event before its ids are "
        f"snapshotted for isolation; observed types: {[e.type for e in first_turn_events]!r}"
    )

    turn_started_at = dt.datetime.now(dt.UTC)
    second_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: SECOND"}],
    }
    sent = await anthropic_client.beta.sessions.events.send(session.id, events=[second_message])
    assert sent.data, "send must echo back the sent event"
    boundary_event_id = sent.data[0].id

    second_idle_event_id = await _wait_for_idle_event_since(
        anthropic_client, session.id, turn_started_at, _IDLE_WAIT_S
    )
    assert second_idle_event_id is not None, (
        f"the second turn's session.status_idle event never appeared within {_IDLE_WAIT_S}s"
    )

    results = [
        event
        async for event in anthropic_client.beta.sessions.events.list(
            session_id=session.id, created_at_gte=turn_started_at, order="asc"
        )
    ]
    result_ids = [event.id for event in results]
    assert boundary_event_id in result_ids, (
        f"a created_at_gte filter anchored on the caller's own pre-send clock reading "
        f"must include the boundary event {boundary_event_id!r}; got {result_ids!r}"
    )
    leaked_first_turn_ids = first_turn_ids.intersection(result_ids)
    assert not leaked_first_turn_ids, (
        f"a pre-send clock bound must exclude every event from the prior turn; "
        f"first-turn events leaked through: {leaked_first_turn_ids!r}"
    )

    typed_results = [
        event
        async for event in anthropic_client.beta.sessions.events.list(
            session_id=session.id,
            created_at_gte=turn_started_at,
            types=["user.message", "agent.message", "session.status_idle"],
            order="asc",
        )
    ]
    assert typed_results, "the typed, bounded listing must return at least the boundary event"
    assert typed_results[0].id == boundary_event_id, (
        f"with created_at_gte and a type filter together, the boundary event "
        f"({boundary_event_id}) must be the first result in asc order; got "
        f"{typed_results[0].id!r} first instead"
    )


async def test_interrupt_from_running_converges_to_terminal(
    anthropic_client: AsyncAnthropic,
    live_bash_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_bash_agent.id, environment_id=live_environment.id
    )
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [
            {"type": "text", "text": "Run exactly: sleep 8\nThen reply with exactly: DONE"}
        ],
    }
    await anthropic_client.beta.sessions.events.send(session.id, events=[message])

    running_status = await _poll_until(
        anthropic_client, session.id, lambda s: s in ("running", "rescheduling"), _RUNNING_WAIT_S
    )
    assert running_status is not None, (
        f"session never reached running/rescheduling within {_RUNNING_WAIT_S}s — cannot "
        "exercise interrupt-from-running without first observing it running"
    )

    terminal = await _interrupt_and_wait_for_terminal(
        anthropic_client, session.id, _TERMINAL_WAIT_S
    )
    assert terminal in ("session.status_idle", "terminated"), (
        f"interrupt from running must converge to a session.status_idle event or terminated; "
        f"observed {terminal!r}"
    )


async def test_interrupt_from_rescheduling_converges_to_terminal_or_skips(
    anthropic_client: AsyncAnthropic,
    live_bash_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_bash_agent.id, environment_id=live_environment.id
    )
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [
            {"type": "text", "text": "Run exactly: sleep 8\nThen reply with exactly: DONE"}
        ],
    }
    await anthropic_client.beta.sessions.events.send(session.id, events=[message])

    rescheduling_status = await _poll_until(
        anthropic_client, session.id, lambda s: s == "rescheduling", _RESCHEDULING_WAIT_S
    )
    if rescheduling_status is None:
        pytest.skip(
            f"session did not enter rescheduling within {_RESCHEDULING_WAIT_S}s for a plain "
            "long-running bash command — rescheduling was not reached deterministically by "
            "this test, so interrupt-from-rescheduling is unverified rather than proven"
        )

    terminal = await _interrupt_and_wait_for_terminal(
        anthropic_client, session.id, _TERMINAL_WAIT_S
    )
    assert terminal in ("session.status_idle", "terminated"), (
        f"interrupt from rescheduling must converge to a session.status_idle event or "
        f"terminated; observed {terminal!r}"
    )


async def test_interrupt_on_idle_session_is_a_noop(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id, environment_id=live_environment.id
    )
    idle_status = await _poll_until(
        anthropic_client, session.id, lambda s: s == "idle", _IDLE_WAIT_S
    )
    assert idle_status is not None, (
        f"a freshly created session with no message sent must reach idle within "
        f"{_IDLE_WAIT_S}s before this test can send an interrupt to it"
    )

    interrupt: BetaManagedAgentsUserInterruptEventParams = {"type": "user.interrupt"}
    await anthropic_client.beta.sessions.events.send(session.id, events=[interrupt])

    retrieved = await anthropic_client.beta.sessions.retrieve(session.id)
    assert retrieved.status == "idle", (
        f"an interrupt sent to an already-idle session must not raise and must leave the "
        f"session idle; observed {retrieved.status!r} afterward"
    )

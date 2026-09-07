"""Contract tests pinning the live MA facts the turn boundary is built on:

(a) a bare `idle` status can be observed immediately after a send — status
    alone is never a valid completion signal;
(b) `sessions.events.list(created_at_gte=...)` is inclusive of the exact
    timestamp it is anchored on, which is what lets the boundary event itself
    be dropped client-side instead of silently missed;
(c)-(e) `user.interrupt` converges to a terminal state from `running`, from
    `rescheduling` (skipped with a reason if that state cannot be reached
    deterministically), and is a no-op when sent to an already-idle session.

Runtime discipline: haiku only, one session per test. Env-gated by
DAIMON_TEST_ANTHROPIC_API_KEY; the default `uv run pytest` deselects the
contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
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


async def test_created_at_gte_boundary_is_inclusive_of_the_boundary_event(
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
    sent = await anthropic_client.beta.sessions.events.send(session.id, events=[message])
    assert sent.data, (
        "send must echo back the sent event so its processed_at can anchor the boundary"
    )
    boundary_event = sent.data[0]
    assert boundary_event.processed_at is not None, (
        "the boundary event must carry a processed_at timestamp to anchor created_at_gte on"
    )

    results = [
        event
        async for event in anthropic_client.beta.sessions.events.list(
            session_id=session.id, created_at_gte=boundary_event.processed_at, order="asc"
        )
    ]
    assert results, (
        "an inclusive created_at_gte filter anchored on the boundary event's own timestamp "
        "must return at least the boundary event itself"
    )
    assert results[0].id == boundary_event.id, (
        f"the boundary event ({boundary_event.id}) must be the first result under an "
        f"inclusive created_at_gte filter in asc order; got {results[0].id!r} first instead "
        "— either the filter is exclusive of the boundary or an earlier event leaked through"
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

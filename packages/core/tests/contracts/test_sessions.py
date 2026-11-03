"""Contract tests for MA session lifecycle: create, event list."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaCloudConfigParams, BetaEnvironment, BetaManagedAgentsAgent

pytestmark = pytest.mark.contract

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@pytest_asyncio.fixture(scope="module")
async def live_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped real agent for session tests. Cleaned up by conftest _cleanup."""
    name = f"contract-test-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract session test"
    )


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped real environment for session tests. Cleaned up by conftest _cleanup."""
    name = f"contract-test-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )


async def test_session_create_returns_expected_shape(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id,
        environment_id=live_environment.id,
    )
    assert session.id.startswith("sesn_"), f"session id prefix must be 'sesn_', got {session.id!r}"
    assert session.status in ("idle", "active"), (
        f"session must have valid status, got {session.status!r}"
    )


async def test_session_event_list_returns_iterable(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id,
        environment_id=live_environment.id,
    )
    events: list[object] = []
    async for e in anthropic_client.beta.sessions.events.list(
        session_id=session.id,
    ):
        events.append(e)
    assert isinstance(events, list), "events.list must return an async iterable"


async def test_a_live_session_carries_only_capabilities_the_unattended_gate_knows(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    """Every unattended turn refuses a session with an agent field, tool type or
    setting the release does not know; the provider's real snapshot must pass,
    or every routine would be refused after an API addition."""
    from daimon.core.turn.unattended_capabilities import require_allowed_capabilities

    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id,
        environment_id=live_environment.id,
    )
    snapshot = await anthropic_client.beta.sessions.retrieve(session.id)
    require_allowed_capabilities(snapshot, allow_skills=True)

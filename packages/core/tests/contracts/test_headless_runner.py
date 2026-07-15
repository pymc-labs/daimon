"""Contract test for `run_turn` against real MA + local daimon-mcp.

This is the Phase 39 canary. It fires one routine end-to-end through
`daimon.core.headless_runner.run_turn` with `mcp_settings`/`account_id`
populated, boots a real local daimon-mcp uvicorn server (via the
`local_daimon_mcp` fixture), and asserts:

1. The MA session was opened with a non-empty `vault_ids` (i.e. the
   per-account daimon-mcp vault was bound — what Phase 38's staging
   regression was missing).
2. No `session.error` event with `error.type == "mcp_connection_failed_error"`
   surfaced during the turn (MA could initialize daimon-mcp).

Env-gated: requires `DAIMON_TEST_ANTHROPIC_API_KEY` and
`DAIMON_DATABASE__TEST_URL`. Default `uv run pytest` skips this — opt-in
via `uv run pytest -m contract`.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaCloudConfigParams, BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.config import McpSettings
from daimon.core.headless_runner import run_turn
from pydantic import HttpUrl, SecretStr

from .conftest import LocalDaimonMCP

pytestmark = pytest.mark.contract

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@pytest_asyncio.fixture(scope="module")
async def live_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped fresh agent. Unique name → session.list is deterministic."""
    name = f"contract-test-headless-runner-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system="contract headless_runner test"
    )


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped fresh environment, cleanup-up by conftest _cleanup."""
    name = f"contract-test-headless-runner-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )


async def test_run_turn_attaches_daimon_mcp_vault(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
    local_daimon_mcp: LocalDaimonMCP,
) -> None:
    """End-to-end canary: run_turn binds a vault and MA initializes daimon-mcp."""
    mcp_settings = McpSettings(
        jwt_secret=SecretStr(local_daimon_mcp.jwt_secret_string),
        public_url=HttpUrl(local_daimon_mcp.public_url),
    )
    account_id = uuid.uuid4()

    await run_turn(
        anthropic=anthropic_client,
        agent_id=live_agent.id,
        environment_id=live_environment.id,
        trigger_message="say hi",
        mcp_settings=mcp_settings,
        account_id=account_id,
    )

    # `live_agent` is module-scoped with a fresh unique name, so the most
    # recent session against it is the one this test just created.
    sessions = [
        s async for s in anthropic_client.beta.sessions.list(agent_id=live_agent.id, limit=1)
    ]
    assert len(sessions) == 1, (
        f"expected exactly one session for fresh agent {live_agent.id!r}, got {len(sessions)}"
    )
    session_id = sessions[0].id

    retrieved = await anthropic_client.beta.sessions.retrieve(session_id)
    assert len(retrieved.vault_ids) >= 1, (
        f"run_turn with mcp_settings must attach at least one vault to the "
        f"MA session; got vault_ids={retrieved.vault_ids!r}"
    )

    # No mcp_connection_failed_error events on this session — MA was able to
    # initialize daimon-mcp using the bound vault's credential.
    async for event in anthropic_client.beta.sessions.events.list(session_id=session_id):
        if event.type == "session.error" and event.error.type == "mcp_connection_failed_error":
            pytest.fail(
                f"session.error with mcp_connection_failed_error surfaced — "
                f"daimon-mcp attach failed: {event.error!r}"
            )

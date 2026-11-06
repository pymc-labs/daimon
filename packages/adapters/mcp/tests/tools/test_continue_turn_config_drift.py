"""An MCP handle follows the agent's setup as a chat thread does, or says it can't.

A chat turn reconciles its session with the agent at every bind
(`prepare_session_for_turn`): identity changes (model, system prompt, skills) replace
the session, mutable ones (tools, MCP servers, .env) update it. `continue_turn` does
not move a conversation to a new session. It refuses a message when the agent's
model, prompt, skills or MCP servers changed, with a message to start a new
conversation, and copies the agent's shared MCP tokens into the open one. These tests change the agent through the MA agent API,
the path the setup tools use, and ask what the next message does.

The chat-side contrast lives in `packages/core/tests/test_session_preparation.py`:
`test_a_model_change_replaces_the_session_and_bills_the_new_model`,
`test_a_tool_added_to_the_agent_is_pushed_to_the_live_session` and
`test_a_new_key_is_swapped_into_the_live_session_without_replacing_it`; and for skills,
`test_session_compat.py` (a `skills_sha256` difference decides `ReplaceSession`).
`test_the_chat_bind_reconciles_what_continue_turn_skips` below pins the same decision
for all three dimensions against the sessions these tests produce.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import agent_chat
from daimon.adapters.mcp.tools.agent_chat import _continue_turn_impl, _start_turn_impl
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_ISOLATED
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.session_compat import (
    DEFAULT_MA_CAPABILITIES,
    ReplaceSession,
    ReuseAsIs,
    UpdateInPlace,
    decide_session_compatibility,
)
from daimon.core.session_snapshot import snapshot_from_retrieved_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from mux.testing.drivers.anthropic.ma_models import EMPTY_CLOUD_CONFIG
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_ENV_NAME = "production"
_ENV_ID = "env_drift"
_M1 = "claude-sonnet-4-6"
_M2 = "claude-opus-5"
_SKILL = {"type": "anthropic", "skill_id": "docx", "version": "latest"}
_SERVER = {"type": "url", "name": "crm", "url": "https://crm.example/mcp"}
_TOOLSET = {
    "type": "mcp_toolset",
    "mcp_server_name": "crm",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}

Dimension = Literal["model", "skills", "mcp_servers"]
Edit = Dimension | Literal["description", "tool_disabled", "skills_reordered"]


class _World:
    """A tenant with one MA agent, the stateful sessions fake, and a connected caller."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.state = FakeSessionsState(ma=FakeMAState())
        self.tenant_id = uuid.uuid4()
        self.account_id = uuid.uuid4()
        self.ma_agent_id = ""
        self.mcp: McpSettings | None = None
        self.fernet: MultiFernet | None = None
        self.client = self._client()

    def _client(self) -> AsyncAnthropic:
        env = {
            "id": _ENV_ID,
            "type": "environment",
            "name": _ENV_NAME,
            "config": EMPTY_CLOUD_CONFIG.model_dump(mode="json"),
            "description": "",
            "metadata": {"daimon_tenant": str(self.tenant_id), "daimon_name": _ENV_NAME},
            "created_at": "2026-06-23T00:00:00Z",
            "updated_at": "2026-06-23T00:00:00Z",
        }

        def environments(request: httpx.Request) -> httpx.Response:
            if (request.method, request.url.path) == ("GET", "/v1/environments"):
                return httpx.Response(200, json=list_response([env]).json())
            raise NotHandled

        return build_fake_anthropic(
            combine_handlers(
                environments,
                make_fake_sessions_handler(self.state),
                make_fake_memory_store_handler(),
                make_fake_ma_handler(self.state.ma),
            )
        )

    async def seed(self) -> None:
        async with self.factory() as db, db.begin():
            tenant = await make_tenant(
                db, platform="discord", workspace_id="drift", id=self.tenant_id
            )
            account = await make_account(db, tenant=tenant)
            self.account_id = account.id
        agent = await self.client.beta.agents.create(
            name="drift-agent",
            model=_M1,
            metadata={"daimon_tenant": str(self.tenant_id), "daimon_name": "drift-agent"},
        )
        self.ma_agent_id = agent.id

    @property
    def auth(self) -> AuthIdentity:
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=self.ma_agent_id),
        )

    @property
    def runtime(self) -> McpRuntime:
        settings = MagicMock()
        settings.tool_safety = OPEN_TOOL_SAFETY
        if self.mcp is None:
            settings.mcp.public_url = None
            settings.mcp.jwt_secret = None
        else:
            settings.mcp = self.mcp
        settings.github.fallback_pat = None
        return McpRuntime(
            session_factory=self.factory,
            client=self.client,  # type: ignore[arg-type]
            settings=settings,  # type: ignore[arg-type]
            deployment_default=DeploymentDefault(environment_name=_ENV_NAME),
            fernet=self.fernet,
        )

    async def current_agent(self) -> BetaManagedAgentsAgent:
        return await self.client.beta.agents.retrieve(self.ma_agent_id)

    async def change_agent(self, dimension: Edit) -> None:
        """Edit the agent through the MA agent API, as the setup tools do."""
        current = await self.current_agent()
        match dimension:
            case "model":
                await self.client.beta.agents.update(
                    self.ma_agent_id, version=current.version, model=_M2
                )
            case "skills":
                await self.client.beta.agents.update(
                    self.ma_agent_id,
                    version=current.version,
                    skills=[_SKILL],  # type: ignore[list-item]
                )
            case "mcp_servers":
                await self.client.beta.agents.update(
                    self.ma_agent_id,
                    version=current.version,
                    mcp_servers=[_SERVER],  # type: ignore[list-item]
                    tools=[_TOOLSET],  # type: ignore[list-item]
                )
            case "tool_disabled":
                await self.client.beta.agents.update(
                    self.ma_agent_id,
                    version=current.version,
                    tools=[
                        {
                            **_TOOLSET,
                            "default_config": {**_TOOLSET["default_config"], "enabled": False},
                        }
                    ],  # type: ignore[list-item,dict-item]
                )
            case "skills_reordered":
                await self.client.beta.agents.update(
                    self.ma_agent_id,
                    version=current.version,
                    skills=list(reversed([s.model_dump(mode="json") for s in current.skills])),  # type: ignore[list-item]
                )
            case "description":
                await self.client.beta.agents.update(
                    self.ma_agent_id, version=current.version, description="Now with notes."
                )

    def message_target(self) -> str:
        """The session the most recent message went to."""
        assert self.state.sent_batches, "no message reached MA"
        return self.state.sent_batches[-1][0]


def _mismatch(dimension: Dimension, session_agent: Any, agent: BetaManagedAgentsAgent) -> str:
    """Empty when the session runs the agent's current value on `dimension`."""
    match dimension:
        case "model":
            ran, want = session_agent.model.id, agent.model.id
        case "skills":
            ran = [s.skill_id for s in session_agent.skills]
            want = [s.skill_id for s in agent.skills]
        case "mcp_servers":
            ran = [s.name for s in session_agent.mcp_servers]
            want = [s.name for s in agent.mcp_servers]
    return "" if ran == want else f"the session ran {ran!r} but the agent now has {want!r}"


@pytest.fixture
async def world(db_session_factory: async_sessionmaker[AsyncSession]) -> _World:
    world = _World(db_session_factory)
    await world.seed()
    return world


async def _start_then_change_then_continue(world: _World, dimension: Dimension) -> str:
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    handle = started["handle"]
    await world.change_agent(dimension)
    await _continue_turn_impl(world.runtime, world.auth, handle, "and now?")
    return handle


@pytest.mark.parametrize("dimension", ["model", "skills", "mcp_servers"])
async def test_a_change_only_a_new_session_runs_is_refused_and_nothing_is_sent(
    world: _World, dimension: Dimension
) -> None:
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    sent = len(world.state.sent_batches)
    await world.change_agent(dimension)

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert len(world.state.sent_batches) == sent
    # The new conversation the refusal asks for runs the new setup.
    fresh = await _start_turn_impl(world.runtime, world.auth, "hello again")
    problem = _mismatch(
        dimension, world.state.sessions[fresh["handle"]].agent, await world.current_agent()
    )
    assert not problem, problem


async def test_an_unchanged_agent_continues_on_the_same_session(world: _World) -> None:
    started = await _start_turn_impl(world.runtime, world.auth, "hello")

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_an_edit_the_session_does_not_run_is_no_reason_to_refuse(world: _World) -> None:
    """A new agent version alone, here a description, leaves the conversation open."""
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("description")

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_a_skill_at_latest_matches_the_version_the_session_resolved(
    world: _World,
) -> None:
    await world.change_agent("skills")
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    # The provider reports the version it resolved `latest` to.
    world.state.sessions[started["handle"]].agent.skills[0].version = "1759000000"
    await world.change_agent("description")

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_a_tool_disabled_on_a_server_the_session_keeps_is_refused(
    world: _World,
) -> None:
    await world.change_agent("mcp_servers")
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("tool_disabled")

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")


async def test_an_edit_to_a_server_hidden_from_the_caller_still_asks_for_a_new_one(
    world: _World,
) -> None:
    """The check compares the agent's definition, not each caller's view of it.

    Rebuilding a session's past view from today's sign-ins can hide a change,
    so an edit to a server only someone else signs in to refuses here too.
    """
    await world.change_agent("mcp_servers")
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("tool_disabled")

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")


async def test_a_server_repointed_to_another_url_is_refused(world: _World) -> None:
    await world.change_agent("mcp_servers")
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    current = await world.current_agent()
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        mcp_servers=[{**_SERVER, "url": "https://other.example/mcp"}],  # type: ignore[list-item]
    )

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")


async def test_reordered_per_tool_settings_are_no_reason_to_refuse(world: _World) -> None:
    configs = [
        {"name": "search", "enabled": True, "permission_policy": {"type": "always_allow"}},
        {"name": "update", "enabled": False, "permission_policy": {"type": "always_ask"}},
    ]
    current = await world.current_agent()
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        mcp_servers=[_SERVER],  # type: ignore[list-item]
        tools=[{**_TOOLSET, "configs": configs}],  # type: ignore[list-item]
    )
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    current = await world.current_agent()
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        tools=[{**_TOOLSET, "configs": list(reversed(configs))}],  # type: ignore[list-item]
    )

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_a_permission_policy_edit_is_no_reason_to_refuse(world: _World) -> None:
    """Every send re-gates permission policies from the current policy."""
    await world.change_agent("mcp_servers")
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    current = await world.current_agent()
    asks = {**_TOOLSET["default_config"], "permission_policy": {"type": "always_ask"}}  # type: ignore[dict-item]
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        tools=[{**_TOOLSET, "default_config": asks}],  # type: ignore[list-item]
    )

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_reordered_skills_are_no_reason_to_refuse(world: _World) -> None:
    current = await world.current_agent()
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        skills=[_SKILL, {**_SKILL, "skill_id": "xlsx"}],  # type: ignore[list-item]
    )
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("skills_reordered")

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def _make_isolated(world: _World) -> None:
    current = await world.current_agent()
    await world.client.beta.agents.update(
        world.ma_agent_id,
        version=current.version,
        metadata={**current.metadata, MA_METADATA_KEY_ISOLATED: "true"},
    )


async def test_marking_the_agent_isolated_is_refused(world: _World) -> None:
    """An isolated agent's session gets no vault, secrets or memory; this one has them."""
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await _make_isolated(world)

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")


async def test_an_isolated_agents_conversation_is_refused_after_a_server_change(
    world: _World,
) -> None:
    await _make_isolated(world)
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("mcp_servers")

    with pytest.raises(ToolError, match="This agent changed since this conversation started"):
        await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")


async def test_an_isolated_agents_conversation_survives_a_description_edit(
    world: _World,
) -> None:
    await _make_isolated(world)
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    await world.change_agent("description")

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert world.message_target() == started["handle"]


async def test_continue_turn_mirrors_the_agents_shared_mcp_tokens_into_the_callers_vault(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token someone replaced on the agent reaches an open conversation.

    The chat bind re-mirrors the caller's vault on every turn
    (`RemirrorVaultCredentials`); `continue_turn` calls the same function.
    """
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    world.state.sessions[started["handle"]].vault_ids = ["vlt_caller"]
    world.mcp = McpSettings(public_url="https://mcp.example/mcp", jwt_secret=SecretStr("s" * 32))
    world.fernet = make_fernet()
    synced: list[dict[str, Any]] = []

    async def no_refresh(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def sync(_client: Any, **kwargs: Any) -> None:
        synced.append(kwargs)

    monkeypatch.setattr(agent_chat, "refresh_mounted_mcp_vaults", no_refresh)
    monkeypatch.setattr(agent_chat, "sync_agent_mcp_credentials", sync)

    await _continue_turn_impl(world.runtime, world.auth, started["handle"], "and now?")

    assert len(synced) == 1
    assert synced[0]["account_id"] == world.account_id
    assert synced[0]["agent_id"] == world.auth.agent_id
    assert synced[0]["tenant_id"] == world.tenant_id


@pytest.mark.parametrize("dimension", ["model", "skills", "mcp_servers"])
async def test_the_chat_bind_reconciles_what_continue_turn_skips(
    world: _World, dimension: Dimension
) -> None:
    """Chat parity: the decision a bind makes for the session `start_turn` froze.

    Compares the frozen session's snapshot with the one the changed agent wants,
    through the same `decide_session_compatibility` that `prepare_session_for_turn` calls.
    """
    started = await _start_turn_impl(world.runtime, world.auth, "hello")
    recorded = snapshot_from_retrieved_session(world.state.sessions[started["handle"]])
    await world.change_agent(dimension)
    # A session created now freezes what a bind would want the caller to run.
    fresh = await _start_turn_impl(world.runtime, world.auth, "hello again")
    desired = snapshot_from_retrieved_session(world.state.sessions[fresh["handle"]])

    decision = decide_session_compatibility(
        recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=_NOW
    )

    assert not isinstance(decision, ReuseAsIs), f"a chat bind would keep the stale {dimension}"
    expected = UpdateInPlace if dimension == "mcp_servers" else ReplaceSession
    assert isinstance(decision, expected), decision

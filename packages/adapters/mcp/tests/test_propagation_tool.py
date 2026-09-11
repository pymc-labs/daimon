"""DB-backed unit tests for the propagation MCP tools.

Tests that admin set/clear persists at workspace and channel scope against real
Postgres (last-write-wins), and that non-admin callers are rejected with no write.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.propagation import (
    _clear_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
    _explain_agent_resolution_impl,  # pyright: ignore[reportPrivateUsage]
    _set_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.routing_facts import build_clear_default_note, build_set_default_note
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_ADMIN_REQUIRED = "requires a workspace or server admin"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


def _admin_auth(*, tenant_id: uuid.UUID, account_id: uuid.UUID | None = None) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN,
        is_admin=True,
    )


def _non_admin_auth(*, tenant_id: uuid.UUID) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        is_admin=False,
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account and return (tenant_id, account_id)."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        return tenant.id, account.id


# ---------------------------------------------------------------------------
# set_agent_default: workspace scope
# ---------------------------------------------------------------------------


async def test_set_agent_default_persists_at_workspace_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    result = await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "writer", None)

    assert result.agent_name == "writer", "result must echo back the agent_name that was set"
    assert result.scope == "workspace", "no channel_id means workspace scope"
    assert result.previous_agent_name is None, "scope had no prior default"
    assert result.routing_note == build_set_default_note(
        agent_name="writer", scope_label="workspace"
    ), "routing_note must equal the core function's output for the same inputs"

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None, "set_agent_default must create a TenantConfig row"
    assert row.agent_name == "writer", "agent_name must be persisted at workspace scope"


async def test_set_agent_default_last_write_wins_at_workspace_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "first-agent", None)
    result = await _set_agent_default_impl(
        _runtime(committing_sessionmaker), auth, "second-agent", None
    )

    assert result.previous_agent_name == "first-agent", (
        "second call must report the overwritten prior agent_name"
    )
    assert result.agent_name == "second-agent", "result must echo the newly-set agent_name"

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None and row.agent_name == "second-agent", (
        "last-write-wins: workspace scope must hold the second agent_name"
    )


# ---------------------------------------------------------------------------
# set_agent_default: channel scope
# ---------------------------------------------------------------------------


async def test_set_agent_default_persists_at_channel_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)
    channel_id = "C123456"

    result = await _set_agent_default_impl(
        _runtime(committing_sessionmaker), auth, "channel-agent", channel_id
    )

    assert result.scope == f"channel:{channel_id}", "result must report the channel scope label"
    assert result.agent_name == "channel-agent", "result must echo back the agent_name"
    assert result.routing_note == build_set_default_note(
        agent_name="channel-agent", scope_label=f"channel:{channel_id}"
    ), "routing_note must equal the core function's output for the same inputs"

    row = await get_scope(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
    )
    assert row is not None, "set_agent_default must create a ChannelConfig row"
    assert row.agent_name == "channel-agent", "agent_name must be persisted at channel scope"


# ---------------------------------------------------------------------------
# clear_agent_default
# ---------------------------------------------------------------------------


async def test_clear_agent_default_removes_workspace_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "to-clear", None)
    result = await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert result.cleared is True, "cleared must be True when a default existed"
    assert result.routing_note == build_clear_default_note(scope_label="workspace", cleared=True), (
        "routing_note must equal the core function's output for the same inputs"
    )

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is None or row.agent_name is None, (
        "agent_name must be gone from workspace scope after clear"
    )


async def test_clear_agent_default_is_idempotent_when_no_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    result = await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert result.cleared is False, "cleared must be False when scope had no agent_name"
    assert result.routing_note == build_clear_default_note(
        scope_label="workspace", cleared=False
    ), "no-op clear's routing_note must equal the core function's output for cleared=False"
    assert build_clear_default_note(
        scope_label="workspace", cleared=False
    ) != build_clear_default_note(scope_label="workspace", cleared=True), (
        "clear_agent_default's note must distinguish cleared from no-op"
    )


# ---------------------------------------------------------------------------
# non-admin rejection: no write performed
# ---------------------------------------------------------------------------


async def test_set_agent_default_raises_for_non_admin_and_performs_no_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, _account_id = await _seed(committing_sessionmaker)
    auth = _non_admin_auth(tenant_id=tenant_id)

    with pytest.raises(ToolError) as exc_info:
        await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "any-agent", None)

    assert _ADMIN_REQUIRED in str(exc_info.value), (
        "non-admin caller must be refused with the expected message"
    )

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is None, "no write must have been performed for a non-admin caller"


async def test_clear_agent_default_raises_for_non_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id = await _seed(committing_sessionmaker)
    auth = _non_admin_auth(tenant_id=tenant_id)

    with pytest.raises(ToolError) as exc_info:
        await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert _ADMIN_REQUIRED in str(exc_info.value), (
        "non-admin caller must be refused with the expected message for clear as well"
    )


# ---------------------------------------------------------------------------
# explain_agent_resolution: which tier wins, and why
# ---------------------------------------------------------------------------


def _runtime_with_default(
    sessionmaker: async_sessionmaker[AsyncSession], agent_name: str
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name=agent_name),
    )


async def test_explain_falls_through_to_the_deployment_default_when_nothing_is_set(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)

    result = await _explain_agent_resolution_impl(
        _runtime_with_default(committing_sessionmaker, "daimon"),
        _admin_auth(tenant_id=tenant_id, account_id=account_id),
        "chan-1",
    )

    assert result.effective_agent_name == "daimon", "deployment default should answer"
    assert result.winning_tier == "deployment", "no channel or workspace row means deployment wins"
    assert result.channel_default is None, "channel tier holds nothing"
    assert result.tenant_default is None, "workspace tier holds nothing"


async def test_explain_reports_the_workspace_tier_when_it_overrides_the_deployment(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The case that made a real QA run measure the wrong agent for weeks.

    An unscoped channel silently inherits a workspace default that differs from
    what defaults/config.yaml ships, so 'we tested the seeded agent' can be false
    while every channel looks untouched.
    """
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)
    runtime = _runtime_with_default(committing_sessionmaker, "daimon")
    await _set_agent_default_impl(runtime, auth, "some-fork", None)

    result = await _explain_agent_resolution_impl(runtime, auth, "chan-1")

    assert result.effective_agent_name == "some-fork", "workspace default outranks deployment"
    assert result.winning_tier == "tenant", "the workspace tier is the one that won"
    assert result.deployment_default == "daimon", (
        "the shipped default must still be reported, so the divergence is visible"
    )
    assert result.channel_default is None, "this channel set nothing of its own"


async def test_explain_reports_the_channel_tier_as_the_narrowest_winner(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)
    runtime = _runtime_with_default(committing_sessionmaker, "daimon")
    await _set_agent_default_impl(runtime, auth, "workspace-agent", None)
    await _set_agent_default_impl(runtime, auth, "channel-agent", "chan-1")

    result = await _explain_agent_resolution_impl(runtime, auth, "chan-1")

    assert result.effective_agent_name == "channel-agent", "channel tier is narrowest and wins"
    assert result.winning_tier == "channel", "winner must be attributed to the channel tier"
    assert result.tenant_default == "workspace-agent", (
        "the overridden workspace value must still be reported"
    )
    assert "chan-1" in result.explanation, "the sentence should name the channel asked about"


async def test_explain_is_readable_by_a_non_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Reading routing is not privileged — members are who ask this question."""
    tenant_id, _ = await _seed(committing_sessionmaker)

    result = await _explain_agent_resolution_impl(
        _runtime_with_default(committing_sessionmaker, "daimon"),
        _non_admin_auth(tenant_id=tenant_id),
        "chan-1",
    )

    assert result.effective_agent_name == "daimon", "a non-admin must still get an answer"


async def test_explain_says_so_when_nothing_resolves(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _ = await _seed(committing_sessionmaker)
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )

    result = await _explain_agent_resolution_impl(
        runtime, _non_admin_auth(tenant_id=tenant_id), "chan-1"
    )

    assert result.effective_agent_name is None, "nothing set at any tier means no agent"
    assert result.winning_tier is None, "no tier can be credited"
    assert "nothing to answer it" in result.explanation, (
        "the sentence must say a mention there goes unanswered, not stay silent"
    )

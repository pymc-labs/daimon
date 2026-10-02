"""archive_isolation_copy: only a closing channel's own copy goes, with its pin and default."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channel_isolation import (
    _set_channel_isolation_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.isolation_copies import (
    _archive_isolation_copy_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
OTHER = "222222222222222222"
COPY = "channel-111111"


class _World:
    def __init__(self, tenant_id: uuid.UUID, account_id: uuid.UUID, state: FakeMAState) -> None:
        self.tenant_id, self.account_id, self.state = tenant_id, account_id, state

    def auth(self, *, admin: bool = True, scopes: frozenset[str] | None = None) -> AuthIdentity:
        return AuthIdentity(
            token_kind=None if scopes is None else "operator",
            scopes=scopes or frozenset(),
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.ADMIN if admin else Role.USER,
            platform="discord",
            platform_user_id="444444444444444444",
            is_admin=admin,
        )

    def archived(self, name: str) -> bool:
        return any(
            a["name"] == name and a.get("archived_at") is not None
            for a in self.state.agents.values()
        )


def _archiving(state: FakeMAState) -> Callable[[httpx.Request], httpx.Response]:
    """The agent fake, plus `POST /v1/agents/{id}/archive` stamping `archived_at`."""
    agents = make_fake_ma_handler(state)

    def handler(request: httpx.Request) -> httpx.Response:
        archive = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or archive is None:
            return agents(request)
        agent = state.agents[archive["id"]]
        agent["archived_at"] = "2026-01-01T00:00:00Z"
        return httpx.Response(200, json=agent)

    return handler


async def _isolated_copy(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[_World, McpRuntime]:
    """ROOM isolated with a copy of ``shared``, which answers in OTHER."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=OTHER),
            tenant_id=tenant.id,
            agent_name="shared",
            mode="agent",
        )
    state = FakeMAState()
    for agent_id, name in (("agent_shared", "shared"), ("agent_daimon", "daimon")):
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant.id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(_archiving(state)),
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    world = _World(tenant.id, account.id, state)
    made = await _set_channel_isolation_impl(
        runtime, world.auth(), channel_id=ROOM, isolated=True, fork_from="shared"
    )
    assert made.agent_name == COPY, made
    return world, runtime


async def test_a_closing_channels_copy_is_archived_with_its_pin_and_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)
    with pytest.raises(ToolError, match="still pinned"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY)
    with pytest.raises(ToolError, match="made for another channel"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY, channel_id=OTHER)
    with pytest.raises(ToolError, match="Only a workspace or server admin"):
        await _archive_isolation_copy_impl(
            runtime, world.auth(admin=False), name=COPY, channel_id=ROOM
        )
    assert not world.archived(COPY), "a refusal archives nothing"

    done = await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY, channel_id=ROOM)
    assert (done.name, done.closed_channel_id) == (COPY, ROOM), done
    assert world.archived(COPY), "the copy is archived"
    async with committing_sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=world.tenant_id)
        _, channels = await list_propagations_for_tenant(session, tenant_id=world.tenant_id)
    assert COPY not in policy.agent_channel_pins, "its pin is gone"
    assert policy.isolated_channel_ids == (ROOM,), "the channel stays isolated, answering nothing"
    assert {row.channel_id: row.agent_name for row in channels} == {OTHER: "shared"}, (
        "its default is cleared, every other one kept"
    )


async def test_only_an_isolation_copy_is_archived_and_never_a_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)
    with pytest.raises(ToolError, match="wasn't made as an isolated channel's copy"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name="shared")
    with pytest.raises(ToolError, match="wasn't made as an isolated channel's copy"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name="daimon")
    with pytest.raises(ToolError, match="no agent by that name"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name="missing")
    with pytest.raises(ToolError, match="list the agents again"):
        await _archive_isolation_copy_impl(
            runtime, world.auth(), name=COPY, channel_id=ROOM, expected_ma_agent_id="agent_old"
        )
    assert not any(world.archived(n) for n in ("shared", "daimon", COPY))


async def test_a_copy_still_answering_elsewhere_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id="333333333333333333"),
            tenant_id=world.tenant_id,
            agent_name=COPY,
            mode="agent",
        )
    with pytest.raises(ToolError, match="still a channel's default"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY, channel_id=ROOM)
    assert not world.archived(COPY)


async def test_a_copy_made_the_workspace_default_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=world.tenant_id),
            tenant_id=world.tenant_id,
            agent_name=COPY,
            mode="agent",
        )
    with pytest.raises(ToolError, match="workspace or deployment default"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY, channel_id=ROOM)
    assert not world.archived(COPY)


async def test_an_operator_token_archives_only_with_agents_archive(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)
    without = world.auth(scopes=frozenset({"channels:write"}))
    with pytest.raises(ToolError, match="does not have the agents:archive scope"):
        await _archive_isolation_copy_impl(runtime, without, name=COPY, channel_id=ROOM)
    assert not world.archived(COPY)

    scoped = world.auth(scopes=frozenset({"agents:archive"}))
    await _archive_isolation_copy_impl(runtime, scoped, name=COPY, channel_id=ROOM)
    assert world.archived(COPY)


async def test_an_upstream_failure_changes_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world, runtime = await _isolated_copy(committing_sessionmaker)

    async def failing(*_args: object, **_kwargs: object) -> object:
        raise anthropic.APIStatusError(
            "boom", response=httpx.Response(500, request=httpx.Request("POST", "/")), body=None
        )

    monkeypatch.setattr(runtime.client.beta.agents, "archive", failing)
    with pytest.raises(ToolError, match="failed upstream"):
        await _archive_isolation_copy_impl(runtime, world.auth(), name=COPY, channel_id=ROOM)
    async with committing_sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=world.tenant_id)
    assert policy.agent_channel_pins == {COPY: (ROOM,)}, "the pin stays with the live copy"

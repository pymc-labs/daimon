"""set_channel_rule and set_agent_rule: who may set them, and what they refuse."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channel_rules import (
    _set_agent_rule_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_rule_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import access_policy
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
OTHER = "222222222222222222"
ISOLATED = "333333333333333333"
OWN = ChannelRule(readers="own", writers="own")
POLICY = TenantAccessPolicy(
    channel_rules={ISOLATED: OWN}, agent_rules={"own": AgentRule(runs_in=(ISOLATED,))}
)


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, McpRuntime]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=POLICY,
        )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(make_fake_ma_handler(FakeMAState())),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    return tenant.id, account.id, runtime


def _auth(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    admin: bool = False,
    administers: frozenset[str] = frozenset(),
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        platform_user_id="444444444444444444",
        is_admin=admin,
        administered_channel_ids=administers,
    )


async def _policy(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> TenantAccessPolicy:
    async with sessionmaker() as session:
        return await load_access_policy(session, tenant_id=tenant_id)


async def test_a_server_admin_limits_and_reopens_a_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)

    done = await _set_channel_rule_impl(
        runtime, admin, channel_id=ROOM, readers="inside", writers="none"
    )
    assert (done.readers, done.writers, done.changed) == ("inside", "none", True), done
    assert "Only turns inside it read it" in done.note, done.note
    again = await _set_channel_rule_impl(runtime, admin, channel_id=ROOM, writers="none")
    assert not again.changed, "repeating the call changes nothing"

    opened = await _set_channel_rule_impl(
        runtime, admin, channel_id=ROOM, readers="any", writers="any"
    )
    assert (opened.readers, opened.writers) == ("any", "any"), opened
    assert await _policy(committing_sessionmaker, tenant_id) == POLICY, "other rules are kept"


async def test_a_channel_admin_sets_no_rule_even_on_their_own_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    channel_admin = _auth(tenant_id, account_id, administers=frozenset({ROOM, ISOLATED}))
    member = _auth(tenant_id, account_id)

    for auth in (channel_admin, member):
        for change in ({"writers": "none"}, {"readers": "inside"}):
            with pytest.raises(ToolError, match="Only a server or workspace admin"):
                await _set_channel_rule_impl(runtime, auth, channel_id=ROOM, **change)  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="Only a server or workspace admin"):
            await _set_channel_rule_impl(runtime, auth, channel_id=ISOLATED, readers="any")
    assert await _policy(committing_sessionmaker, tenant_id) == POLICY, "refusals write nothing"


async def test_a_channel_kept_to_its_own_agents_keeps_them_until_its_readers_change(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)
    with pytest.raises(ToolError, match="so it keeps them"):
        await _set_channel_rule_impl(runtime, admin, channel_id=ISOLATED, release_agents=True)
    closed = await _set_channel_rule_impl(runtime, admin, channel_id=ISOLATED, writers="none")
    assert (closed.readers, closed.writers) == ("own", "none"), "readers stay own"
    with pytest.raises(ToolError, match="Pass readers, writers or release_agents"):
        await _set_channel_rule_impl(runtime, admin, channel_id=ROOM)
    released = await _set_channel_rule_impl(
        runtime, admin, channel_id=ISOLATED, readers="any", writers="any", release_agents=True
    )
    assert released.released_agents == ["own"], released
    assert await _policy(committing_sessionmaker, tenant_id) == TenantAccessPolicy()


async def test_set_agent_rule_needs_a_server_admin_and_an_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    channel_admin = _auth(tenant_id, account_id, administers=frozenset({ROOM}))
    with pytest.raises(ToolError, match="Only a server or workspace admin"):
        await _set_agent_rule_impl(runtime, channel_admin, agent_name="own", runs_in=[ROOM])
    admin = _auth(tenant_id, account_id, admin=True)
    with pytest.raises(ToolError, match="There is no agent named ghost"):
        await _set_agent_rule_impl(runtime, admin, agent_name="ghost", runs_in=[ROOM])
    assert await _policy(committing_sessionmaker, tenant_id) == POLICY, "refusals write nothing"


async def test_a_protection_change_waits_out_a_held_policy_fence(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A channel tidy holds the shared policy fence; the write waits instead of failing busy."""
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)
    async with committing_sessionmaker.begin() as holder:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock_shared("
                "hashtextextended(current_schema() || ':' || :key, 0))"
            ),
            {"key": access_policy._policy_write_key(tenant_id)},  # pyright: ignore[reportPrivateUsage]
        )
        writer = asyncio.create_task(
            _set_channel_rule_impl(runtime, admin, channel_id=ROOM, writers="none")
        )
        await asyncio.sleep(0.3)
        assert not writer.done(), "the write must wait for the fence, not fail as busy"
    done = await asyncio.wait_for(writer, 10)
    assert (done.writers, done.changed) == ("none", True), done


@pytest.mark.parametrize(
    ("before", "after", "widens"),
    [
        ((ROOM,), (ROOM, OTHER), True),
        ((ROOM,), None, True),
        ((ROOM, OTHER), (ROOM,), False),
        ((ROOM,), (), False),
        (None, (ROOM,), False),
    ],
)
def test_only_a_rule_that_adds_a_place_widens(
    before: tuple[str, ...] | None, after: tuple[str, ...] | None, widens: bool
) -> None:
    from daimon.adapters.mcp.tools.channel_rules import (
        _widens,  # pyright: ignore[reportPrivateUsage]
    )

    assert _widens(before, after) is widens


@pytest.mark.parametrize(
    ("runs_in", "come_too"), [((ROOM, OTHER, "555555555555555555"), True), ((ROOM,), False)]
)
async def test_widening_an_agent_rule_says_its_repos_come_too(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    runs_in: tuple[str, ...],
    come_too: bool,
) -> None:
    from types import SimpleNamespace

    from daimon.adapters.mcp.tools import channel_rules as tools
    from daimon.core.channel_rules import AgentRuleChange
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.github_connected_repos import AgentRepoSummary

    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(agent_rules={"bot": AgentRule(runs_in=(ROOM, OTHER))}),
        )

    async def set_rule(*_args: object, **_kwargs: object) -> AgentRuleChange:
        return AgentRuleChange("bot", runs_in, True)

    async def find(_client: object, *, tenant_id: uuid.UUID, name: str) -> object:
        return SimpleNamespace(id="ag_bot", metadata={})

    async def summary(_session: object, *, tenant_id: uuid.UUID) -> AgentRepoSummary:
        bot = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_bot")
        return AgentRepoSummary(pairs=1, agents=1, by_agent={bot: ("ana/thesis",)})

    monkeypatch.setattr(tools, "set_agent_rule", set_rule)
    monkeypatch.setattr(tools, "find_agent_by_daimon_tag", find)
    monkeypatch.setattr(tools, "agent_repo_summary", summary)
    result = await _set_agent_rule_impl(
        runtime,
        _auth(tenant_id, account_id, admin=True),
        agent_name="bot",
        runs_in=list(runs_in),
    )
    assert result.note.endswith("bot's repos come too.") is come_too

"""Confirming repos for an agent reads the person's roles live, never from the group cache."""

from __future__ import annotations

import httpx
from daimon.adapters.mcp.auth.group_members import DiscordMembers, GroupLookups
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_admins import GroupMembersCache
from daimon.core.github_panel import requester_manages_agent
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_platform_role_ids
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD, USER, ROLE, CHANNEL = (
    "111111111111111111",
    "222222222222222222",
    "333333333333333333",
    "444444444444444444",
)


async def test_a_removed_role_is_refused_inside_the_cache_window(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id=GUILD)
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="discord", external_id=USER, tenant=tenant, account=account
        )
        await set_platform_role_ids(s, account.id, [ROLE])
        await set_channel_admins(
            s,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=CHANNEL,
            role_ids=[ROLE],
            user_ids=[],
            actor_account_id=None,
        )
        await set_access_policy(
            s,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(agent_channel_pins={"TeamA": (CHANNEL,)}),
        )
    # Discord says the member holds the role, then that it was taken away.
    answers = [
        httpx.Response(200, json={"roles": [ROLE]}),
        httpx.Response(200, json={"roles": [ROLE]}),
        httpx.Response(200, json={"roles": []}),
        httpx.Response(500),
    ]
    asked: list[httpx.Request] = []

    def discord(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answers[min(len(asked), len(answers)) - 1]

    lookups = GroupLookups(
        sessionmaker=sessionmaker,
        fernet=None,
        teams_client=None,
        discord=DiscordMembers(
            "bot-token", httpx.AsyncClient(transport=httpx.MockTransport(discord))
        ),
        cache=GroupMembersCache(ttl_s=60, failure_ttl_s=15),
    )

    async def manages() -> bool:
        live = lookups.live_members("discord", GUILD)
        assert live is not None
        async with sessionmaker() as s:
            return await requester_manages_agent(
                s,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                platform_user_id=USER,
                agent_name="TeamA",
                ma_agent_id="ma_team_a",
                default=DeploymentDefault(),
                is_daimon_managed=False,
                members=live,
            )

    cached = lookups.members("discord", GUILD)
    assert cached is not None
    assert await cached(USER) == frozenset({ROLE}), "the cache saw the role"
    assert await manages() is True, "while they hold it, they manage TeamA"
    assert await manages() is False, "removed since: the live read refuses"
    assert await cached(USER) == frozenset({ROLE}), "the cache still holds the old answer"
    assert len(asked) == 3, "each live check asked Discord; the cached read did not"
    assert await manages() is False, "a failed live read refuses too"

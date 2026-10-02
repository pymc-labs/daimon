"""Group grants: Slack user groups and Teams team owners, looked up through a short cache."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from daimon.core.channel_admins import (
    GroupLookupFailed,
    GroupMembersCache,
    load_member_group_ids,
    member_group_ids,
)
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


def _fetcher(
    members: dict[str, frozenset[str]],
) -> tuple[list[str], Callable[[str], Awaitable[frozenset[str]]]]:
    asked: list[str] = []

    async def fetch(group_id: str) -> frozenset[str]:
        asked.append(group_id)
        if group_id not in members:
            raise GroupLookupFailed("no such group")
        return members[group_id]

    return asked, fetch


async def test_member_group_ids_keeps_the_groups_that_admit_the_user_and_fails_closed() -> None:
    asked, fetch = _fetcher({"S1": frozenset({"U1"}), "S2": frozenset({"U2"})})

    matched = await member_group_ids("U1", ["S2", "S1", "S_GONE", "S1"], fetch)

    assert matched == frozenset({"S1"}), (
        "only a group listing the user; a failed lookup grants nothing"
    )
    assert asked == ["S1", "S2", "S_GONE"], "each group asked once"


async def test_the_cache_keeps_members_for_its_ttl_and_never_keeps_a_failure() -> None:
    now = [0.0]
    cache = GroupMembersCache(ttl_s=60, clock=lambda: now[0])
    calls: list[str] = []
    fail = [True]

    async def fetch() -> frozenset[str]:
        calls.append("fetch")
        if fail[0]:
            raise GroupLookupFailed("rate limited")
        return frozenset({"U1"})

    with pytest.raises(GroupLookupFailed):
        await cache.members(("slack", "S1"), fetch)
    fail[0] = False
    assert await cache.members(("slack", "S1"), fetch) == frozenset({"U1"}), "asked again"
    now[0] = 59
    assert await cache.members(("slack", "S1"), fetch) == frozenset({"U1"})
    assert len(calls) == 2, "a failure is not kept; a success is, inside the ttl"
    now[0] = 61
    await cache.members(("slack", "S1"), fetch)
    assert len(calls) == 3, "an expired entry is looked up again"


async def test_the_cache_stays_bounded() -> None:
    cache = GroupMembersCache(ttl_s=60, max_entries=2, clock=lambda: 0.0)

    async def fetch() -> frozenset[str]:
        return frozenset()

    for group in ("A", "B", "C"):
        await cache.members((group,), fetch)
    assert len(cache._entries) <= 2, "full of live entries, it starts again"  # pyright: ignore[reportPrivateUsage]


async def test_only_groups_a_grant_names_are_looked_up(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0GROUPS")
    asked, fetch = _fetcher({"S1": frozenset({"U1"}), "S9": frozenset({"U1"})})

    none = await load_member_group_ids(
        db_session, tenant_id=tenant.id, platform="slack", platform_user_id="U1", members=fetch
    )
    assert (none, asked) == (frozenset(), []), "no group grant, no lookup"

    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="C1",
        role_ids=["S1"],
        user_ids=["U7"],
        actor_account_id=None,
    )
    matched = await load_member_group_ids(
        db_session, tenant_id=tenant.id, platform="slack", platform_user_id="U1", members=fetch
    )
    assert (matched, asked) == (frozenset({"S1"}), ["S1"]), "S9 is in no grant, so never asked"

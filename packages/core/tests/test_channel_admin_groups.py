"""Group grants: Slack user groups and Teams team owners, looked up through a short cache."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from daimon.core.channel_admins import (
    GroupLookupFailed,
    GroupMembersCache,
    channel_admin_user_ids,
    load_member_group_ids,
    load_stored_subject,
    member_group_ids,
)
from daimon.core.stores.accounts import set_platform_role_ids
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import TenantRow
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
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


async def _stored_member(
    session: AsyncSession, tenant: TenantRow, user_id: str, groups: list[str]
) -> uuid.UUID:
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform=tenant.platform, external_id=user_id, tenant=tenant, account=account
    )
    await set_platform_role_ids(session, account.id, groups)
    return account.id


async def _grant(session: AsyncSession, tenant: TenantRow, group: str, users: list[str]) -> None:
    await set_channel_admins(
        session,
        tenant_id=tenant.id,
        platform=tenant.platform,
        channel_id="C1",
        role_ids=[group],
        user_ids=users,
        actor_account_id=None,
    )


async def test_a_stored_slack_group_grants_only_while_a_live_lookup_admits_the_person(
    db_session: AsyncSession,
) -> None:
    """Any member may edit a Slack user group by default: outside a turn, look again."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0STORED")
    account_id = await _stored_member(db_session, tenant, "U1", ["S1"])
    await _grant(db_session, tenant, "S1", [])

    async def administered(members: Callable[[str], Awaitable[frozenset[str]]] | None) -> set[str]:
        subject = await load_stored_subject(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            account_id=account_id,
            platform_user_id="U1",
            members=members,
        )
        return set(subject.administered_channel_ids)

    _, still_in = _fetcher({"S1": frozenset({"U1"})})
    _, left = _fetcher({"S1": frozenset({"U2"})})
    _, failing = _fetcher({})
    assert await administered(still_in) == {"C1"}, "still in the group: still the channel's admin"
    assert await administered(left) == set(), "left the group: no longer, before their next turn"
    assert await administered(failing) == set(), "a failed lookup grants nothing"
    assert await administered(None) == set(), "no lookup at all grants nothing"


async def test_stored_discord_roles_stand_without_a_lookup(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="111111111111111")
    account_id = await _stored_member(db_session, tenant, "222222222222222", ["r1"])
    await _grant(db_session, tenant, "r1", [])

    subject = await load_stored_subject(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        account_id=account_id,
        platform_user_id="222222222222222",
    )
    assert subject.administered_channel_ids == frozenset({"C1"}), (
        "Discord sends roles with every event and guards them with Manage Roles"
    )


async def test_channel_admin_dms_reach_a_stored_slack_group_member_only_while_still_in_it(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0DMS")
    await _stored_member(db_session, tenant, "U_STAYED", ["S1"])
    await _stored_member(db_session, tenant, "U_LEFT", ["S1"])
    await _grant(db_session, tenant, "S1", ["U_GRANTED"])

    async def recipients(members: Callable[[str], Awaitable[frozenset[str]]] | None) -> list[str]:
        found = await channel_admin_user_ids(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            limit=10,
            members=members,
        )
        assert found is not None, "the channel has a grant"
        return found

    _, live = _fetcher({"S1": frozenset({"U_STAYED"})})
    assert await recipients(live) == ["U_GRANTED", "U_STAYED"], (
        "a member who left the group no longer hears the channel's requests"
    )
    assert await recipients(None) == ["U_GRANTED"], "without a lookup only granted users"


async def test_channel_admin_dms_cap_after_dropping_members_who_left_the_group(
    db_session: AsyncSession,
) -> None:
    """Members who left sort first; a cap before the live re-check would keep only them."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0CAP")
    for n in range(3):
        await _stored_member(db_session, tenant, f"U_A_LEFT{n}", ["S1"])
    await _stored_member(db_session, tenant, "U_Z_STAYED", ["S1"])
    await _grant(db_session, tenant, "S1", [])
    _, live = _fetcher({"S1": frozenset({"U_Z_STAYED"})})

    found = await channel_admin_user_ids(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C1", limit=2, members=live
    )

    assert found == ["U_Z_STAYED"], "the current member is reached however many have left"

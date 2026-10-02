"""Slack user group lookups for channel admin grants: members, listing, and failing closed."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from daimon.adapters.slack.channel_admin_groups import (
    channel_admin_caller,
    fetch_user_group_members,
    list_user_groups,
)
from daimon.core.channel_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.testing.factories import make_tenant
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _Client:
    """Answers `usergroups.users.list` and `usergroups.list`, counting calls."""

    def __init__(self, members: dict[str, list[str]], *, error: str | None = None) -> None:
        self.members = members
        self.error = error
        self.calls: list[str] = []

    def _fail(self) -> None:
        if self.error is not None:
            raise SlackApiError(self.error, {"ok": False, "error": self.error})

    async def usergroups_users_list(self, *, usergroup: str) -> dict[str, Any]:
        self.calls.append(usergroup)
        self._fail()
        return {"ok": True, "users": self.members.get(usergroup, [])}

    async def usergroups_list(self) -> dict[str, Any]:
        self._fail()
        return {
            "ok": True,
            "usergroups": [
                {"id": "S1", "handle": "leads", "name": "Leads"},
                {"id": "S2", "handle": "", "name": "Ops"},
                {"handle": "no-id"},
            ],
        }


def _as_client(client: _Client) -> AsyncWebClient:
    return client  # type: ignore[return-value]


async def test_members_and_listing_read_slacks_answer() -> None:
    client = _as_client(_Client({"S1": ["U1", "U2"]}))
    assert await fetch_user_group_members(client, "S1") == frozenset({"U1", "U2"})
    assert await list_user_groups(client) == {"S1": "@leads (Leads)", "S2": "Ops"}, (
        "labelled by handle and name; an entry without an id is skipped"
    )


async def test_a_missing_scope_is_a_failed_lookup() -> None:
    client = _as_client(_Client({}, error="missing_scope"))
    with pytest.raises(GroupLookupFailed):
        await fetch_user_group_members(client, "S1")
    with pytest.raises(GroupLookupFailed):
        await list_user_groups(client)


async def test_the_caller_holds_the_grant_named_groups_they_are_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0GROUPS")
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="C1",
        role_ids=["S1", "S2"],
        user_ids=[],
        actor_account_id=None,
    )
    await db_session.commit()
    runtime = MagicMock(sessionmaker=db_session_factory, group_members=GroupMembersCache())
    fake = _Client({"S1": ["U1"], "S2": ["U9"]})

    caller = await channel_admin_caller(
        runtime, _as_client(fake), tenant_id=tenant.id, user_id="U1", is_admin=False
    )
    again = await channel_admin_caller(
        runtime, _as_client(fake), tenant_id=tenant.id, user_id="U1", is_admin=False
    )
    admin = await channel_admin_caller(
        runtime, _as_client(fake), tenant_id=tenant.id, user_id="U1", is_admin=True
    )

    assert caller.role_ids == frozenset({"S1"}) == again.role_ids, "only the group listing U1"
    assert sorted(fake.calls) == ["S1", "S2"], "the second call is served from the cache"
    assert (admin.role_ids, admin.is_server_admin) == (frozenset(), True), "an admin needs none"


async def test_a_failed_lookup_admits_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0GROUPS")
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="C1",
        role_ids=["S1"],
        user_ids=[],
        actor_account_id=None,
    )
    await db_session.commit()
    runtime = MagicMock(sessionmaker=db_session_factory, group_members=GroupMembersCache())

    caller = await channel_admin_caller(
        runtime,
        _as_client(_Client({"S1": ["U1"]}, error="ratelimited")),
        tenant_id=tenant.id,
        user_id="U1",
        is_admin=False,
    )

    assert caller.role_ids == frozenset(), "fail closed"

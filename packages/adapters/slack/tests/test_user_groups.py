"""Slack user group lookups for DM grants: members, failures and the cache."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from daimon.adapters.slack.user_groups import fetch_user_group_members, user_group_members
from daimon.core.native_admins import GroupLookupFailed, GroupMembersCache

_USERS = re.compile(r"https://slack\.com/api/usergroups\.users\.list.*")


def _calls(fake: Any) -> int:
    return sum(len(v) for k, v in fake.mock.requests.items() if "usergroups.users" in str(k[1]))


async def test_fetch_user_group_members_when_ok_then_returns_user_ids(
    fake_slack_web_client: Any,
) -> None:
    fake_slack_web_client.mock.get(_USERS, payload={"ok": True, "users": ["U1", "U2"]})
    members = await fetch_user_group_members(fake_slack_web_client.client, "S1")
    assert members == frozenset({"U1", "U2"}), "the group's user ids"


@pytest.mark.parametrize(
    "reply",
    [
        {"payload": {"ok": False, "error": "missing_scope"}},
        {"payload": {"ok": True}},
        {"exception": aiohttp.ClientConnectionError("down")},
    ],
)
async def test_fetch_user_group_members_when_slack_fails_then_raises_lookup_failed(
    fake_slack_web_client: Any, reply: dict[str, Any]
) -> None:
    fake_slack_web_client.mock.get(_USERS, **reply)
    with pytest.raises(GroupLookupFailed):
        await fetch_user_group_members(fake_slack_web_client.client, "S1")


async def test_user_group_members_when_asked_twice_then_cached_unless_fresh(
    fake_slack_web_client: Any,
) -> None:
    fake_slack_web_client.mock.get(_USERS, payload={"ok": True, "users": ["U1"]}, repeat=True)
    runtime: Any = SimpleNamespace(group_members=GroupMembersCache())
    client = fake_slack_web_client.client
    cached = user_group_members(runtime, client, team_id="T1")
    await cached("S1")
    await cached("S1")
    assert _calls(fake_slack_web_client) == 1, "delivery reuses the cached members"
    await user_group_members(runtime, client, team_id="T1", fresh=True)("S1")
    assert _calls(fake_slack_web_client) == 2, "a conversation's start looks up afresh"

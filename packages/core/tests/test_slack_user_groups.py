"""Slack user group lookups for DM grants: members, failures and the cache."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.native_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.slack_user_groups import fetch_user_group_members, user_group_members


def _client(reply: httpx.Response | Exception, calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_fetch_user_group_members_when_ok_then_returns_user_ids() -> None:
    calls: list[httpx.Request] = []
    reply = httpx.Response(200, json={"ok": True, "users": ["U1", "U2"]})
    async with _client(reply, calls) as http:
        members = await fetch_user_group_members(http, token="xoxb-t", group_id="S1")
    assert members == frozenset({"U1", "U2"}), "the group's user ids"
    assert calls[0].url.params["usergroup"] == "S1", "asks for that group"
    assert calls[0].headers["Authorization"] == "Bearer xoxb-t", "with the bot token"


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json=["U1"]),
        httpx.Response(200, text="not json"),
        httpx.Response(500, json={"ok": True, "users": []}),
        httpx.ConnectError("down"),
    ],
)
async def test_fetch_user_group_members_when_slack_fails_then_raises_lookup_failed(
    reply: httpx.Response | Exception,
) -> None:
    async with _client(reply, []) as http:
        with pytest.raises(GroupLookupFailed):
            await fetch_user_group_members(http, token="t", group_id="S1")


async def test_user_group_members_when_asked_twice_then_cached_unless_fresh() -> None:
    calls: list[httpx.Request] = []
    cache = GroupMembersCache()
    async with _client(httpx.Response(200, json={"ok": True, "users": ["U1"]}), calls) as http:
        cached = user_group_members(http, token="t", team_id="T1", cache=cache)
        await cached("S1")
        await cached("S1")
        assert len(calls) == 1, "delivery reuses the cached members"
        fresh = user_group_members(http, token="t", team_id="T1", cache=cache, fresh=True)
        await fresh("S1")
        assert len(calls) == 2, "a conversation's start looks up afresh"
        await user_group_members(http, token="t", team_id="T1")("S1")
        assert len(calls) == 3, "no cache: every ask reads Slack"

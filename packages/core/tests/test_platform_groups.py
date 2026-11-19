"""Live group lists for the DM picker: Discord roles and Slack user groups."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.native_admins import GroupLookupFailed
from daimon.core.platform_groups import (
    PlatformGroup,
    discord_roles,
    merge_groups,
    slack_user_groups,
)


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_discord_roles_when_listed_then_rank_order_without_everyone_and_bots() -> None:
    seen: list[httpx.Request] = []

    def roles(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"id": "g", "name": "@everyone", "position": 0},
                {"id": "r1", "name": "Staff", "position": 1},
                {"id": "r2", "name": "Bot", "position": 5, "managed": True},
                {"id": "r3", "name": "Leads", "position": 4},
            ],
        )

    async with _client(httpx.MockTransport(roles)) as http:
        found = await discord_roles(http, token="t", guild_id="g")

    assert found == (PlatformGroup("r3", "Leads"), PlatformGroup("r1", "Staff")), found
    assert seen[0].headers["Authorization"] == "Bot t", "the bot token is sent"


async def test_slack_user_groups_when_scope_is_missing_then_raises_with_slacks_error() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer ok":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "usergroups": [
                        {"id": "S2", "name": "support"},
                        {"id": "S1", "name": "Admins"},
                        {"id": "S3", "handle": "eng"},
                    ],
                },
            )
        return httpx.Response(200, json={"ok": False, "error": "missing_scope"})

    async with _client(httpx.MockTransport(answer)) as http:
        found = await slack_user_groups(http, token="ok")
        with pytest.raises(GroupLookupFailed) as missing:
            await slack_user_groups(http, token="old")

    assert [g.id for g in found] == ["S1", "S3", "S2"], "sorted by name, handle when unnamed"
    assert str(missing.value) == "missing_scope", "Slack's error names the missing scope"


async def test_group_lookup_when_the_platform_fails_then_raises() -> None:
    async with _client(httpx.MockTransport(lambda _: httpx.Response(503))) as http:
        with pytest.raises(GroupLookupFailed):
            await discord_roles(http, token="t", guild_id="g")


def test_merge_groups_when_a_stored_id_is_gone_then_it_follows_without_a_name() -> None:
    live = (PlatformGroup("b", "B"), PlatformGroup("a", "A"))

    merged = merge_groups(live, ["z", "a", "y"])

    assert [(g.id, g.name) for g in merged] == [("b", "B"), ("a", "A"), ("y", None), ("z", None)], (
        "live first in their order, then unknown stored ids sorted"
    )

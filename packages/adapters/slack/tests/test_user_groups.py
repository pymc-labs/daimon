"""The Slack adapter's user group lookup: the shared one, on the runtime's client and cache."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
from daimon.adapters.slack.user_groups import user_group_lookup
from daimon.core.dm_access import resolve_groups
from daimon.core.native_admins import GroupMembersCache


async def test_user_group_lookup_when_asked_then_uses_the_runtime_client_cache_and_token() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        users = ["U1"] if request.url.params["usergroup"] == "S1" else ["U2"]
        return httpx.Response(200, json={"ok": True, "users": users})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        runtime: Any = SimpleNamespace(http_client=http, group_members=GroupMembersCache())
        client: Any = SimpleNamespace(token="xoxb-t")
        lookup = user_group_lookup(runtime, client, team_id="T1")
        wanted = frozenset({"S1", "S2"})
        tenant_id = uuid.uuid4()
        assert await resolve_groups(lookup, "U1", wanted, tenant_id=tenant_id) == {"S1"}
        assert await resolve_groups(lookup, "U1", wanted, tenant_id=tenant_id) == {"S1"}
    assert len(calls) == 2, "one read per group; the second ask is cached"
    assert calls[0].headers["Authorization"] == "Bearer xoxb-t", "the workspace's bot token"

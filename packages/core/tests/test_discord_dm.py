"""Best-effort Discord DM and role-name reads, against a fake transport."""

from __future__ import annotations

import json

import httpx
from daimon.core.discord_dm import DiscordRest


def _rest(handler: httpx.MockTransport) -> DiscordRest:
    return DiscordRest("token", transport=handler)


async def test_send_dm_opens_a_channel_then_posts_without_mentions() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, json={"id": "77"})
        return httpx.Response(200, json={"id": "1"})

    assert await _rest(httpx.MockTransport(handle)).send_dm("42", "hi")
    assert calls[0][1] == {"recipient_id": "42"}
    assert calls[1][0].endswith("/channels/77/messages")
    assert calls[1][1] == {"content": "hi", "allowed_mentions": {"parse": []}}


async def test_send_dm_reports_failure_instead_of_raising() -> None:
    blocked = httpx.MockTransport(lambda _r: httpx.Response(403, json={"code": 50007}))
    assert not await _rest(blocked).send_dm("42", "hi")
    assert not await _rest(blocked).send_dm("not-an-id", "hi")


async def test_role_names_maps_ids_and_is_empty_on_failure() -> None:
    ok = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=[{"id": "1", "name": "Staff"}, {"id": 2}])
    )
    assert await _rest(ok).role_names("5") == {"1": "Staff"}
    down = httpx.MockTransport(lambda _r: httpx.Response(500))
    assert await _rest(down).role_names("5") == {}

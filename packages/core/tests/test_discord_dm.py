"""Discord DM and role-name reads, against a fake transport."""

from __future__ import annotations

import json

import httpx
import pytest
from daimon.core.discord_dm import DiscordRest, DiscordRestError


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

    await _rest(httpx.MockTransport(handle)).send_dm("42", "hi")
    assert calls[0][1] == {"recipient_id": "42"}, "the DM channel is opened for the user"
    assert calls[1][0].endswith("/channels/77/messages"), "the message goes to that channel"
    assert calls[1][1] == {"content": "hi", "allowed_mentions": {"parse": []}}, "no pings"


async def test_send_dm_raises_when_discord_refuses_or_the_id_is_bad() -> None:
    blocked = httpx.MockTransport(lambda _r: httpx.Response(403, json={"code": 50007}))
    with pytest.raises(DiscordRestError, match="HTTP 403"):
        await _rest(blocked).send_dm("42", "hi")
    with pytest.raises(DiscordRestError, match="decimal"):
        await _rest(blocked).send_dm("not-an-id", "hi")


async def test_role_names_maps_ids_and_raises_when_unreadable() -> None:
    ok = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=[{"id": "1", "name": "Staff"}, {"id": 2}])
    )
    assert await _rest(ok).role_names("5") == {"1": "Staff"}, "malformed roles are skipped"
    with pytest.raises(DiscordRestError, match="HTTP 500"):
        await _rest(httpx.MockTransport(lambda _r: httpx.Response(500))).role_names("5")
    with pytest.raises(DiscordRestError, match="decimal"):
        await _rest(ok).role_names("guild")


async def test_malformed_replies_raise() -> None:
    no_channel: tuple[object, ...] = ([], "x", 7, {"id": 5}, {"id": "abc"})
    for body in no_channel:
        odd = httpx.MockTransport(lambda _r, b=body: httpx.Response(200, json=b))
        with pytest.raises(DiscordRestError, match="no channel id"):
            await _rest(odd).send_dm("42", "hi")
    not_a_list: tuple[object, ...] = ({}, "x")
    for body in not_a_list:
        odd = httpx.MockTransport(lambda _r, b=body: httpx.Response(200, json=b))
        with pytest.raises(DiscordRestError, match="not a list"):
            await _rest(odd).role_names("5")
    mixed = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=[1, "a", None, {"id": "1", "name": "Staff"}])
    )
    assert await _rest(mixed).role_names("5") == {"1": "Staff"}, "only well-formed roles count"
    garbled = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(DiscordRestError, match="JSONDecodeError"):
        await _rest(garbled).send_dm("42", "hi")
    with pytest.raises(DiscordRestError, match="JSONDecodeError"):
        await _rest(garbled).role_names("5")


async def test_unreachable_discord_raises() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DiscordRestError, match="ConnectError"):
        await _rest(httpx.MockTransport(down)).send_dm("42", "hi")


async def test_errors_never_carry_the_token_or_message_text() -> None:
    secret = "tok-SECRET-value"
    failing = httpx.MockTransport(lambda _r: httpx.Response(500, text="tok-SECRET-value body-text"))
    rest = DiscordRest(secret, transport=failing)
    with pytest.raises(DiscordRestError) as dm:
        await rest.send_dm("42", "private message text")
    with pytest.raises(DiscordRestError) as roles:
        await rest.role_names("5")
    for exc in (dm.value, roles.value):
        assert secret not in str(exc) and "body-text" not in str(exc), "no token or reply body"
        assert "private message text" not in str(exc), "no message text"

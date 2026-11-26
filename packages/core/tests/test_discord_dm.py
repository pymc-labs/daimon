"""The scheduler's Discord DMs (error card, opening a DM), against a fake transport."""

from __future__ import annotations

import json

import httpx
import pytest
from daimon.core.discord_membership import open_discord_dm, send_discord_error_dm


async def _send(handler: httpx.MockTransport, user_id: str = "42", text: str = "hi") -> None:
    await send_discord_error_dm("token", user_id, text, "do this", transport=handler)


async def test_send_opens_a_channel_then_posts_one_card_without_mentions() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/users/@me/channels"):
            return httpx.Response(200, json={"id": "77"})
        return httpx.Response(200, json={"id": "1"})

    await _send(httpx.MockTransport(handle))
    assert calls[0][1] == {"recipient_id": "42"}, "the DM channel is opened for the user"
    assert calls[1][0].endswith("/channels/77/messages"), "the message goes to that channel"
    assert calls[1][1] == {
        "embeds": [{"title": "hi", "description": "do this", "color": 0xE74C3C}],
        "allowed_mentions": {"parse": []},
    }, "one error card, no pings"


async def test_send_raises_when_discord_refuses_or_the_id_is_bad() -> None:
    blocked = httpx.MockTransport(lambda _r: httpx.Response(403, json={"code": 50007}))
    with pytest.raises(httpx.HTTPStatusError):
        await _send(blocked)
    with pytest.raises(ValueError, match="decimal"):
        await _send(blocked, user_id="not-an-id")


@pytest.mark.parametrize("body", [[], "x", 7, {"id": 5}, {"id": "abc"}])
async def test_a_reply_without_a_channel_id_raises(body: object) -> None:
    with pytest.raises(ValueError, match="no DM channel id"):
        await _send(httpx.MockTransport(lambda _r: httpx.Response(200, json=body)))


async def test_garbled_or_unreachable_discord_raises() -> None:
    garbled = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(ValueError):
        await _send(garbled)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError):
        await _send(httpx.MockTransport(down))


async def test_errors_never_carry_the_token_or_message_text() -> None:
    failing = httpx.MockTransport(lambda _r: httpx.Response(500, text="tok-SECRET body-text"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await send_discord_error_dm("tok-SECRET", "42", "private text", "x", transport=failing)
    for leaked in ("tok-SECRET", "body-text", "private text"):
        assert leaked not in str(raised.value), "no token, reply body or message text"


async def test_open_makes_one_request_for_the_dm_channel_and_sends_nothing() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "77", "type": 1})

    assert await open_discord_dm("token", "42", transport=httpx.MockTransport(handle)) == "77"
    assert calls == [("POST", "/api/v10/users/@me/channels", {"recipient_id": "42"})], (
        "one open, no message"
    )


@pytest.mark.parametrize(
    ("reply", "raised"),
    [
        (httpx.Response(403, json={"code": 50007}), httpx.HTTPStatusError),
        (httpx.Response(429, json={"retry_after": 1.5}), httpx.HTTPStatusError),
        (httpx.Response(200, json={"id": "abc"}), ValueError),
        (httpx.Response(200, content=b"<html>"), ValueError),
    ],
    ids=["refused", "rate-limited", "malformed", "garbled"],
)
async def test_open_raises_on_a_refusal_a_rate_limit_or_a_bad_reply_without_the_token(
    reply: httpx.Response, raised: type[Exception]
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return reply

    with pytest.raises(raised) as caught:
        await open_discord_dm("tok-SECRET", "42", transport=httpx.MockTransport(handle))
    assert "tok-SECRET" not in str(caught.value), "the token never reaches the error"
    assert len(calls) == 1, "no retry and no send"
    with pytest.raises(ValueError, match="decimal"):
        await open_discord_dm("tok-SECRET", "not-an-id", transport=httpx.MockTransport(handle))
    assert len(calls) == 1, "a bad user id reaches no endpoint"

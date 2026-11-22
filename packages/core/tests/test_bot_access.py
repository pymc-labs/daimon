"""The bot-access check mirrors what delivery needs, and only a lockout counts as waiting."""

from types import SimpleNamespace

import httpx
import pytest
from daimon.core.bot_access import bot_access_blocked, is_missing_access
from daimon.core.channel_visibility import (
    DISCORD_SEND_MESSAGES,
    DISCORD_SEND_MESSAGES_IN_THREADS,
    DISCORD_VIEW_CHANNEL,
)

GUILD, BOT = "424242", "999"
SETTINGS = SimpleNamespace(
    discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "token"))
)
_LOCKED = (403, {"code": 50001, "message": "Missing Access"})


def _channel(cid, *, kind=0, parent=None, guild=GUILD):
    return (
        200,
        {
            "id": cid,
            "type": kind,
            "guild_id": guild,
            "parent_id": parent,
            "permission_overwrites": [],
        },
    )


async def _blocked(channels, *, bits=DISCORD_VIEW_CHANNEL | DISCORD_SEND_MESSAGES, dest="10"):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10/")
        if path == "users/@me":
            return httpx.Response(200, json={"id": BOT})
        if path == f"guilds/{GUILD}":
            roles = [{"id": GUILD, "permissions": str(bits)}]
            return httpx.Response(200, json={"id": GUILD, "owner_id": "1", "roles": roles})
        if path == f"guilds/{GUILD}/members/{BOT}":
            return httpx.Response(200, json={"user": {"id": BOT}, "roles": []})
        if path.startswith("channels/") and path.removeprefix("channels/") in channels:
            status, body = channels[path.removeprefix("channels/")]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"code": 10003})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        return await bot_access_blocked(http, SETTINGS, guild_id=GUILD, destination_id=dest)


@pytest.mark.parametrize("kind", [0, 5])
async def test_a_text_or_announcement_channel_the_bot_can_post_in_is_not_blocked(kind):
    """Delivery posts in both types, and Embed Links is not needed."""
    assert not await _blocked({"10": _channel("10", kind=kind)}), "bot may post"


async def test_a_thread_under_a_forum_needs_send_messages_in_threads():
    channels = {"20": _channel("20", kind=11, parent="10"), "10": _channel("10", kind=15)}
    with_threads = DISCORD_VIEW_CHANNEL | DISCORD_SEND_MESSAGES_IN_THREADS

    assert not await _blocked(channels, bits=with_threads, dest="20"), "forum thread is fine"
    assert await _blocked(channels, dest="20"), "Send Messages alone is not enough in a thread"


async def test_a_channel_without_send_messages_is_blocked():
    assert await _blocked({"10": _channel("10")}, bits=DISCORD_VIEW_CHANNEL), "cannot send"


async def test_missing_access_is_a_wait_for_the_channel_and_for_a_thread_parent():
    assert await _blocked({"10": _LOCKED}), "403 Missing Access on the channel"
    channels = {"20": _channel("20", kind=11, parent="10"), "10": _LOCKED}
    assert await _blocked(channels, dest="20"), "403 on the thread's parent"


@pytest.mark.parametrize(
    "channels",
    [
        {},
        {"10": (200, {"id": "10", "type": 0, "guild_id": "other"})},
        {"10": _channel("10", kind=2)},
        {"10": (403, {"code": 20001})},
    ],
    ids=["not_found", "other_guild", "voice_channel", "other_403"],
)
async def test_other_failures_are_not_waiting(channels):
    """A 404, another server's channel or any other refusal keeps its old handling."""
    assert not await _blocked(channels), "not a bot lockout"


def test_only_a_403_with_a_missing_access_code_counts():
    assert is_missing_access(403, 50001) and is_missing_access(403, 50013)
    assert not is_missing_access(404, 50001) and not is_missing_access(403, 10003)

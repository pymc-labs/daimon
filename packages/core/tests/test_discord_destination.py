"""The REST destination resolver applies the owner's own Discord permissions."""

import httpx
import pytest
from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_SEND_MESSAGES,
    DISCORD_SEND_MESSAGES_IN_THREADS,
    DISCORD_VIEW_CHANNEL,
)
from daimon.core.discord_destination import DiscordRestDestinationResolver
from daimon.core.routine_service import Placement, RoutineRefused

GUILD, OWNER = "424242", "123"
SPEAK = DISCORD_VIEW_CHANNEL | DISCORD_SEND_MESSAGES | DISCORD_SEND_MESSAGES_IN_THREADS


def _channel(cid, *, kind=0, parent=None, guild=GUILD, overwrites=()):
    return {
        "id": cid,
        "type": kind,
        "guild_id": guild,
        "parent_id": parent,
        "permission_overwrites": list(overwrites),
    }


def _deny_everyone(bits):
    return {"id": GUILD, "type": 0, "allow": "0", "deny": str(bits)}


def _resolver(channels, *, everyone=SPEAK, thread_members=(), owner_id="999"):
    """A resolver over canned REST answers: guild, roles, member and the given channels."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10/")
        if path == f"guilds/{GUILD}/members/{OWNER}":
            return httpx.Response(200, json={"user": {"id": OWNER}, "roles": []})
        if path == f"guilds/{GUILD}":
            return httpx.Response(200, json={"id": GUILD, "owner_id": owner_id})
        if path == f"guilds/{GUILD}/roles":
            return httpx.Response(200, json=[{"id": GUILD, "permissions": str(everyone)}])
        if path.startswith("channels/") and "/thread-members/" in path:
            cid = path.split("/")[1]
            ok = (cid, path.rsplit("/", 1)[1]) in thread_members
            return httpx.Response(200 if ok else 404, json={})
        if path.startswith("channels/") and path.removeprefix("channels/") in channels:
            return httpx.Response(200, json=channels[path.removeprefix("channels/")])
        return httpx.Response(404, json={})

    return DiscordRestDestinationResolver(
        "token", guild_id=GUILD, owner_id=OWNER, transport=httpx.MockTransport(handle)
    )


async def test_channel_the_owner_can_post_in_resolves_with_its_category():
    """A text channel resolves to itself, with the category it sits in."""
    resolver = _resolver({"10": _channel("10", parent="77")})

    placement = await resolver("channel", "10")

    assert placement == Placement(channel_id="10", category_id="77"), "channel placement"


async def test_thread_resolves_through_its_parent_channel():
    """A thread's permissions and category come from its parent, and the budget is the parent."""
    channels = {"20": _channel("20", kind=11, parent="10"), "10": _channel("10", parent="77")}

    placement = await _resolver(channels)("thread", "20")

    assert placement.parent_channel_id == "10", "thread parent"
    assert placement.budget_channel_id == "10", "spend counts against the parent"
    assert placement.category_id == "77", "category read from the parent"


@pytest.mark.parametrize(
    ("kind", "channel_id", "message"),
    [("thread", "10", "is a channel"), ("channel", "20", "is a thread")],
)
async def test_wrong_kind_is_refused(kind, channel_id, message):
    """Naming a channel as a thread, or the reverse, is refused with the right word."""
    channels = {"20": _channel("20", kind=11, parent="10"), "10": _channel("10")}

    with pytest.raises(RoutineRefused, match=message):
        await _resolver(channels)(kind, channel_id)


async def test_owner_who_cannot_send_is_refused():
    """A channel the owner can see but not write in is refused, naming the owner."""
    channel = _channel("10", overwrites=[_deny_everyone(DISCORD_SEND_MESSAGES)])

    with pytest.raises(RoutineRefused, match="owner can't post in 10"):
        await _resolver({"10": channel})("channel", "10")


async def test_channel_in_another_guild_is_refused():
    """An id from another server never resolves, even if the bot can see it."""
    with pytest.raises(RoutineRefused, match="not in this server"):
        await _resolver({"10": _channel("10", guild="1")})("channel", "10")


async def test_unknown_channel_reads_as_not_visible():
    """A missing channel (or one the bot cannot read) is the same refusal."""
    with pytest.raises(RoutineRefused, match="cannot see 10"):
        await _resolver({})("channel", "10")


async def test_administrator_may_post_where_a_rule_denies_sending():
    """Administrators hold every bit, as delivery treats them."""
    channel = _channel("10", overwrites=[_deny_everyone(DISCORD_SEND_MESSAGES)])

    placement = await _resolver({"10": channel}, everyone=DISCORD_ADMINISTRATOR)("channel", "10")

    assert placement.channel_id == "10", "administrator resolves"


async def test_guild_owner_may_post_where_a_rule_denies_sending():
    """The server's owner holds every bit."""
    channel = _channel("10", overwrites=[_deny_everyone(DISCORD_SEND_MESSAGES)])

    placement = await _resolver({"10": channel}, owner_id=OWNER)("channel", "10")

    assert placement.channel_id == "10", "server owner resolves"


@pytest.mark.parametrize(("member", "allowed"), [(True, True), (False, False)])
async def test_private_thread_needs_membership(member, allowed):
    """A private thread needs the owner to be in it, as delivery requires."""
    channels = {"20": _channel("20", kind=12, parent="10"), "10": _channel("10")}
    resolver = _resolver(channels, thread_members={("20", OWNER)} if member else ())

    if allowed:
        assert (await resolver("thread", "20")).channel_id == "20", "member resolves"
    else:
        with pytest.raises(RoutineRefused, match="can't post in 20"):
            await resolver("thread", "20")

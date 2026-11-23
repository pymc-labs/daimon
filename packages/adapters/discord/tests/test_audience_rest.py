"""Our own REST read of a server: all or nothing, bounded, and polite to Discord's rate
limits. Discord is a fake (`httpx.MockTransport`); waits are a few milliseconds."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from daimon.adapters.discord.audience_rest import (
    MEMBER_PAGE,
    ReadFailed,
    rooms_from_snapshot,
    snapshot_from_payloads,
)
from daimon.core.audience_facts import AudienceMember, RoomFacts

from . import audience_fakes as fake

NOW = datetime(2026, 5, 1, tzinfo=UTC)
ROLES = (str(fake.CORE), str(fake.FRIEND))


def _facts(snapshot: Any) -> dict[str, RoomFacts]:
    rooms = rooms_from_snapshot(snapshot, internal_role_ids=ROLES, team_role_ids=(str(fake.CORE),))
    return {cid: RoomFacts(r.facts.contained, r.facts.narrow) for cid, r in rooms.items()}


async def test_a_read_turns_raw_payloads_into_room_facts_and_pages_to_the_end() -> None:
    server = fake.RestServer()
    timed_out = fake.rest_member(21, fake.FRIEND)
    timed_out["communication_disabled_until"] = (NOW + timedelta(hours=1)).isoformat()
    server.members.append(timed_out)
    discord_api = fake.RestDiscord(server)

    snapshot = await discord_api.reader().read(fake.GUILD, now=NOW)

    assert _facts(snapshot) == {
        "1": RoomFacts(False, False),
        "2": RoomFacts(True, False),
        "3": RoomFacts(False, True),
    }, "the outsider sees room 2; the active private thread takes its parent's facts"
    assert (
        rooms_from_snapshot(snapshot, internal_role_ids=ROLES, team_role_ids=())["3"].parent_id
        == "1"
    )
    by_id = {m.user_id: m for m in snapshot.members}
    assert by_id["10"].is_owner and by_id["40"].is_bot and by_id["21"].timed_out
    assert by_id["20"] == AudienceMember("20", frozenset({str(fake.FRIEND)}))
    assert snapshot.member_pages == 1

    server.members = [fake.rest_member(1000 + n, fake.CORE) for n in range(MEMBER_PAGE)]
    server.owner_id = 1000
    snapshot = await discord_api.reader().read(fake.GUILD, now=NOW)
    assert len(snapshot.members) == MEMBER_PAGE
    assert snapshot.member_pages == 2, "exactly 1,000 members: a second, empty page ends it"


def _payloads(server: fake.RestServer) -> dict[str, Any]:
    pages: list[object] = [server.payload("members", {"after": "0", "limit": "1000"})]
    return {
        "guild": server.payload("guild", {}),
        "channels": server.payload("channels", {}),
        "threads": server.payload("threads", {}),
        "member_pages": pages,
    }


def _break(path: str) -> Callable[[dict[str, Any]], None]:
    """Delete or change one field in the payloads."""

    def apply(payloads: dict[str, Any]) -> None:
        *head, last = path.split(".")
        target: Any = payloads
        for key in head:
            target = target[int(key)] if key.isdigit() else target[key]
        if last.isdigit():
            del target[int(last)]
        else:
            del target[last]

    return apply


def _set(change: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    return change


@pytest.mark.parametrize(
    ("label", "change", "reason"),
    [
        ("no owner", _break("guild.owner_id"), "owner_id_missing"),
        ("role without bits", _break("guild.roles.1.permissions"), "role_permissions_missing"),
        ("no @everyone", _break("guild.roles.0"), "everyone_role_missing"),
        ("channel without name", _break("channels.0.name"), "channel_name_missing"),
        ("no overwrites", _break("channels.1.permission_overwrites"), "overwrites_missing"),
        (
            "overwrite without allow",
            _break("channels.0.permission_overwrites.0.allow"),
            "overwrite_allow_missing",
        ),
        ("thread without parent", _break("threads.threads.0.parent_id"), "thread_parent_missing"),
        ("member without roles", _break("member_pages.0.6.roles"), "roles_missing"),
        (
            "a role the role list lacks",
            _set(lambda p: p["member_pages"][0][6]["roles"].append("4999")),
            "member_role_unresolved",
        ),
        ("the owner is not listed", _break("member_pages.0.0"), "owner_not_a_member"),
        (
            "members out of order",
            _set(lambda p: p["member_pages"][0].reverse()),
            "member_order",
        ),
        (
            "a full last page",
            _set(
                lambda p: p["member_pages"].__setitem__(
                    0,
                    [
                        fake.rest_member(10 if n == 0 else 1000 + n, fake.CORE)
                        for n in range(MEMBER_PAGE)
                    ],
                )
            ),
            "member_pages_incomplete",
        ),
        ("no pages", _set(lambda p: p["member_pages"].clear()), "member_pages_incomplete"),
    ],
)
def test_a_snapshot_with_a_missing_or_unresolvable_field_is_rejected(
    label: str, change: Callable[[dict[str, Any]], None], reason: str
) -> None:
    payloads = copy.deepcopy(_payloads(fake.RestServer()))
    snapshot_from_payloads(str(fake.GUILD), now=NOW, **copy.deepcopy(payloads))  # whole: fine
    change(payloads)
    with pytest.raises(ReadFailed) as failed:
        snapshot_from_payloads(str(fake.GUILD), now=NOW, **payloads)
    assert failed.value.reason == reason, label


async def test_a_failed_member_page_or_a_lost_answer_fails_the_whole_read() -> None:
    server = fake.RestServer()
    server.members = [fake.rest_member(1000 + n, fake.CORE) for n in range(MEMBER_PAGE + 5)]
    server.owner_id = 1000
    discord_api = fake.RestDiscord(server)

    async def second_page_down(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(503) if request.url.params["after"] != "0" else None

    server.hooks["members"] = second_page_down
    with pytest.raises(ReadFailed) as failed:
        await discord_api.reader(attempts=2).read(fake.GUILD, now=NOW)
    assert failed.value.reason == "attempts_exhausted"
    assert server.reads["members"] == 3, "page one, then page two twice"

    async def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Missing Access"})

    server.hooks = {"channels": forbidden}
    with pytest.raises(ReadFailed) as failed:
        await discord_api.reader().read(fake.GUILD, now=NOW)
    assert failed.value.reason == "http_403", "no retry for a refusal"


async def test_a_read_never_outlives_its_deadline() -> None:
    server = fake.RestServer()
    discord_api = fake.RestDiscord(server)
    never = asyncio.Event()

    async def hang(_request: httpx.Request) -> None:
        await never.wait()

    server.hooks["members"] = hang
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ReadFailed) as failed:
        await discord_api.reader(scan_deadline_s=0.05).read(fake.GUILD, now=NOW)
    assert failed.value.reason == "deadline"
    assert loop.time() - started < 0.5


def _limited(*, is_global: bool, retry_after: float) -> httpx.Response:
    return httpx.Response(
        429,
        json={
            "message": "You are being rate limited.",
            "retry_after": retry_after,
            "global": is_global,
        },
        headers={"X-RateLimit-Scope": "global" if is_global else "user"},
    )


async def test_429_cooldowns_are_shared_kept_between_reads_and_survive_a_cancel() -> None:
    """A route 429 holds back only its route, a global one every route of every server.
    The cooldowns are plain numbers on the reader, so a read cancelled while it waits
    leaves them standing, and the next read waits out the rest instead of hanging."""
    one, two = fake.RestServer(), fake.RestServer(4001)
    discord_api = fake.RestDiscord(one, two)
    reader = discord_api.reader(attempts=2)
    loop = asyncio.get_running_loop()
    sent: list[tuple[str, float]] = []
    first = [True]

    async def route_limited(request: httpx.Request) -> httpx.Response | None:
        sent.append(("members", loop.time()))
        if first[0]:
            first[0] = False
            return _limited(is_global=False, retry_after=0.03)
        return None

    one.hooks["members"] = route_limited
    started = loop.time()
    await reader.read(fake.GUILD, now=NOW)
    assert len(sent) == 2 and sent[1][1] - sent[0][1] >= 0.03, "the retry waited out the route"
    assert reader.limits.global_until < started, "a route limit is not global"

    # A global 429 on server one holds back server two's read, which is cancelled while
    # it waits; the next read still waits out the rest, then reaches Discord.
    async def global_limited(_request: httpx.Request) -> httpx.Response:
        return _limited(is_global=True, retry_after=0.04)

    one.hooks = {"guild": global_limited}
    with pytest.raises(ReadFailed):
        await reader.read(fake.GUILD, now=NOW)
    until = reader.limits.global_until
    assert until > loop.time()
    before = len(discord_api.log)
    waiting = asyncio.create_task(reader.read(4001, now=NOW))
    await asyncio.sleep(0.01)
    assert len(discord_api.log) == before, "server two waits for the global cooldown"
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert reader.limits.global_until == until, "the cancel left the cooldown standing"

    snapshot = await reader.read(4001, now=NOW)
    assert loop.time() >= until and snapshot.guild_id == "4001", "then it reads, not hangs"
    assert len(discord_api.log) == before + 4

    # A cooldown longer than the read's deadline fails the read at once.
    reader_two = fake.RestDiscord(two).reader(scan_deadline_s=1)
    reader_two.limits.global_until = loop.time() + 10
    started = loop.time()
    with pytest.raises(ReadFailed) as failed:
        await reader_two.read(4001, now=NOW)
    assert failed.value.reason == "rate_limited" and loop.time() - started < 0.5


async def test_a_waiting_request_honours_a_cooldown_that_grew_while_it_slept() -> None:
    """Another server's global 429 can lengthen the cooldown while a request waits out the
    old one: it waits again instead of going early."""
    server = fake.RestServer()
    reader = fake.RestDiscord(server).reader()
    loop = asyncio.get_running_loop()
    sent: list[float] = []

    async def record(_request: httpx.Request) -> None:
        sent.append(loop.time())

    for route in ("guild", "channels", "threads", "members"):
        server.hooks[route] = record
    reader.limits.global_until = loop.time() + 0.02
    reading = asyncio.create_task(reader.read(fake.GUILD, now=NOW))
    await asyncio.sleep(0.01)
    extended = reader.limits.global_until = loop.time() + 0.04  # a 429 elsewhere, meanwhile
    await reading
    assert len(sent) == 4 and min(sent) >= extended, "no request before the longer cooldown"


async def test_an_exhausted_bucket_cools_its_route_before_the_next_request() -> None:
    server = fake.RestServer()
    server.members = [fake.rest_member(1000 + n, fake.CORE) for n in range(MEMBER_PAGE)]
    server.owner_id = 1000
    discord_api = fake.RestDiscord(server)
    loop = asyncio.get_running_loop()
    sent: list[float] = []

    async def last_in_bucket(request: httpx.Request) -> httpx.Response:
        sent.append(loop.time())
        answer = server.payload("members", request.url.params)
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.03"}
        return httpx.Response(200, json=answer, headers=headers)

    server.hooks["members"] = last_in_bucket
    await discord_api.reader().read(fake.GUILD, now=NOW)
    assert len(sent) == 2 and sent[1] - sent[0] >= 0.03

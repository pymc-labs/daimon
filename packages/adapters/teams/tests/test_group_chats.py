"""Group chat rosters: answered only while everyone there is ours."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.adapters.teams.group_chats import ChatMember, GroupChatRoster, outsiders, turn_guard
from microsoft_teams.api import ConversationUpdateActivity

OURS = str(uuid.UUID(int=1))
ADA = ChatMember("29:ada", str(uuid.UUID(int=2)), OURS, "user")
BOT = ChatMember("28:bot", None, None, None)
GUEST = ChatMember("29:guest", str(uuid.UUID(int=3)), OURS, "guest")


@pytest.mark.parametrize(
    ("member", "count"),
    [
        (ADA, 0),
        (BOT, 0),
        (ChatMember("29:x", str(uuid.UUID(int=4)), str(uuid.UUID(int=99)), "user"), 1),
        (GUEST, 1),
        (ChatMember("29:anon", None, None, "anonymous"), 1),
        (ChatMember("29:nobody", str(uuid.UUID(int=5)), OURS, None), 1),
        (ChatMember("29:noid", None, OURS, "user"), 1),
    ],
)
def test_only_our_users_and_bots_are_not_outsiders(member: ChatMember, count: int) -> None:
    assert outsiders([ADA, member], tenant_id=OURS, member_guests=frozenset()) == count


def test_a_guest_is_ours_when_listed_or_guests_are_unrestricted() -> None:
    listed = frozenset({str(uuid.UUID(int=3))})
    assert outsiders([GUEST], tenant_id=OURS, member_guests=listed) == 0
    assert outsiders([GUEST], tenant_id=OURS, member_guests=None) == 0


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_the_roster_is_cached_briefly_and_a_failure_closes_the_chat() -> None:
    calls: list[str] = []
    rosters: list[list[ChatMember] | Exception] = [
        [ADA],
        httpx.ConnectError("down"),
        [ADA, GUEST],
    ]

    async def members(chat_id: str) -> list[ChatMember]:
        calls.append(chat_id)
        roster = rosters.pop(0)
        if isinstance(roster, Exception):
            raise roster
        return roster

    async def guests() -> frozenset[str]:
        return frozenset()

    clock = _Clock()
    roster = GroupChatRoster(tenant_id=OURS, members=members, member_guests=guests, clock=clock)
    assert not await roster.has_outsiders("19:c@thread.v2")
    assert not await roster.has_outsiders("19:c@thread.v2"), "cached"
    assert calls == ["19:c@thread.v2"]
    clock.now = 61
    assert await roster.has_outsiders("19:c@thread.v2"), "an unreadable roster fails closed"
    clock.now = 122
    assert await roster.has_outsiders("19:c@thread.v2"), "a guest joined"
    assert len(calls) == 3


async def test_a_join_drops_the_cached_roster_and_a_fresh_read_skips_the_cache() -> None:
    """Someone added to the chat is seen at once, not after the cache expires."""
    rosters = [[ADA], [ADA, GUEST], [ADA]]

    async def members(_chat_id: str) -> list[ChatMember]:
        return rosters.pop(0)

    async def guests() -> frozenset[str]:
        return frozenset()

    roster = GroupChatRoster(tenant_id=OURS, members=members, member_guests=guests)
    assert not await roster.has_outsiders("19:c@thread.v2")
    update = ConversationUpdateActivity.model_validate(
        {
            "type": "conversationUpdate",
            "id": "u1",
            "channelId": "msteams",
            "from": {"id": "29:ada"},
            "recipient": {"id": "28:bot"},
            "conversation": {"id": "19:c@thread.v2", "conversationType": "groupChat"},
            "membersAdded": [{"id": "29:guest"}],
        }
    )
    chained: list[bool] = []

    async def next_handler() -> None:
        chained.append(True)

    ctx = SimpleNamespace(activity=update, next=next_handler)
    await roster.on_conversation_update(cast(Any, ctx))
    assert chained, "the other conversationUpdate handlers still run"
    assert await roster.has_outsiders("19:c@thread.v2"), "the join is seen at once"
    assert not await roster.has_outsiders("19:c@thread.v2", fresh=True), "fresh reads again"
    assert not rosters, "every check past the join read the roster"


@pytest.mark.parametrize("closed", [False, True])
async def test_a_turn_guard_stops_the_turn_and_tells_the_asker_once_the_chat_closes(
    closed: bool,
) -> None:
    """A closed chat cancels the turn and tells the asker; an open one changes nothing."""
    told: list[bool] = []

    async def members(_chat_id: str) -> list[ChatMember]:
        return [ADA, GUEST] if closed else [ADA]

    async def guests() -> frozenset[str]:
        return frozenset()

    roster = GroupChatRoster(tenant_id=OURS, members=members, member_guests=guests)

    async def tell() -> None:
        told.append(True)

    cancel = asyncio.Event()
    still_ours = turn_guard(roster, "19:c@thread.v2", cancel, tell)
    assert await still_ours(True) is not closed
    assert cancel.is_set() is closed, "only a closed chat stops the turn"
    assert bool(told) is closed, "only a closed chat tells the asker"
    assert not await turn_guard(None, "19:c@thread.v2", asyncio.Event(), tell)(False), (
        "no roster fails closed"
    )


async def test_a_turn_guard_tells_the_asker_once_and_stays_closed() -> None:
    """Every later check of a closed turn fails at once: no second read, no second notice."""
    reads: list[str] = []
    told: list[bool] = []

    async def members(chat_id: str) -> list[ChatMember]:
        reads.append(chat_id)
        return [ADA, GUEST]

    async def guests() -> frozenset[str]:
        return frozenset()

    async def tell() -> None:
        told.append(True)

    roster = GroupChatRoster(tenant_id=OURS, members=members, member_guests=guests)
    still_ours = turn_guard(roster, "19:c@thread.v2", asyncio.Event(), tell)
    assert [await still_ours(False), await still_ours(True), await still_ours(False)] == [
        False,
        False,
        False,
    ], "closed for the rest of the turn"
    assert told == [True], "the asker is told once per turn"
    assert reads == ["19:c@thread.v2"], "a closed turn reads the roster no more"

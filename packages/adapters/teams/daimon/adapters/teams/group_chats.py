"""Who is in a Teams group chat: answers there only while everyone is ours.

Everyone in a group chat sees each answer, so Daimon answers only while the
whole roster is the host organisation's: members with our tenant and the
"user" role. Anyone else (another tenant, a guest unless the tenant's access
policy lists them or guests are unrestricted, an anonymous or unidentified
member) closes the chat, as does a roster that cannot be read. Bots are not
counted. Rosters are cached for a minute, and dropped when someone joins. A
running turn checks again before it shows any of its work (a draft, tool
lines, a confirmation or question card), and afresh before its answer: a chat
that has closed meanwhile stops the turn (`turn_guard`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

import structlog
from daimon.adapters.teams.externals import LOOKUP_ERRORS, MemberGuestsFetch
from daimon.adapters.teams.externals import (
    _TtlCache as TtlCache,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.teams.identity import TeamsInbound, canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from microsoft_teams.api import ConversationUpdateActivity
from microsoft_teams.apps import ActivityContext

log = structlog.get_logger()

OUTSIDERS_PRESENT = (
    "I don't answer in this chat: someone in it is from outside this organisation "
    "and would see my answers. Ask me in our 1:1 chat instead."
)
ROSTER_TTL_S: Final = 60.0
ROSTER_TIMEOUT_S: Final = 5.0


@dataclass(frozen=True)
class ChatMember:
    """One roster entry, as Bot Framework lists it."""

    id: str
    aad_object_id: str | None
    tenant_id: str | None
    user_role: str | None


# Chat id -> its roster. Raises on failure.
ChatMembersFetch = Callable[[str], Awaitable[Sequence[ChatMember]]]


def outsiders(
    members: Sequence[ChatMember], *, tenant_id: str, member_guests: frozenset[str] | None
) -> int:
    """How many members are not ours; `member_guests` None lets every guest in."""
    count = 0
    for member in members:
        if member.id.startswith("28:"):
            continue  # a bot, this one included
        role = (member.user_role or "").lower()
        user = canonical_uuid(member.aad_object_id)
        ours = canonical_uuid(member.tenant_id) == tenant_id and user is not None
        guest_ok = role == "guest" and (member_guests is None or user in member_guests)
        if not ours or not (role == "user" or guest_ok):
            count += 1
    return count


class GroupChatRoster:
    """Whether a group chat holds anyone from outside the organisation; never raises."""

    def __init__(
        self,
        *,
        tenant_id: str,
        members: ChatMembersFetch,
        member_guests: MemberGuestsFetch,
        restrict_guests: bool = True,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = ROSTER_TIMEOUT_S,
    ) -> None:
        self._tenant_id = canonical_uuid(tenant_id) or tenant_id
        self._members = members
        self._member_guests = member_guests
        self._restrict_guests = restrict_guests
        self._timeout_s = timeout_s
        self._open = TtlCache[str, bool](clock)

    async def has_outsiders(self, chat_id: str, *, fresh: bool = False) -> bool:
        """True unless the roster reads and every member is ours; `fresh` skips the cache."""
        cached = None if fresh else self._open.get(chat_id)
        if cached is not None:
            return cached[0]
        try:
            members = await asyncio.wait_for(self._members(chat_id), self._timeout_s)
            guests = None
            if self._restrict_guests:
                guests = frozenset(
                    (canonical_uuid(g) or g)
                    for g in await asyncio.wait_for(self._member_guests(), self._timeout_s)
                )
            found = outsiders(members, tenant_id=self._tenant_id, member_guests=guests)
        except LOOKUP_ERRORS as err:
            log.info("teams.group_chat.roster_failed", error=type(err).__name__)
            found = 1
        if found:
            log.info("teams.group_chat.outsiders", count=found)
        self._open.put(chat_id, bool(found), ROSTER_TTL_S)
        return bool(found)

    async def on_conversation_update(
        self, ctx: ActivityContext[ConversationUpdateActivity]
    ) -> None:
        """Drop a group chat's cached roster when someone joins it."""
        activity = ctx.activity
        if activity.conversation.conversation_type == "groupChat" and activity.members_added:
            self._open.pop(activity.conversation.id.split(";", 1)[0])
        await ctx.next()


# A turn's `turn_guard`: True while the chat is still ours (`fresh`: uncached).
RosterCheck = Callable[[bool], Awaitable[bool]]


def turn_guard(
    roster: GroupChatRoster | None,
    chat_id: str,
    cancel: asyncio.Event,
    tell: Callable[[], Awaitable[None]],
) -> RosterCheck:
    """A group-chat turn's check before it shows its work, card or answer.

    False once anyone from outside is in the chat, and from then on: the first
    such check stops the turn and tells the asker (`tell`), once per turn.
    """
    closed = False

    async def still_ours(fresh: bool) -> bool:
        nonlocal closed
        if closed:
            return False
        if roster is not None and not await roster.has_outsiders(chat_id, fresh=fresh):
            return True
        if closed:  # Another check closed it meanwhile and told the asker.
            return False
        closed = True
        cancel.set()
        with contextlib.suppress(*TEAMS_SEND_ERRORS):
            await tell()
        return False

    return still_ours


async def placed(roster: GroupChatRoster | None, inbound: TeamsInbound) -> TeamsInbound:
    """`inbound` as from outside unless everyone in its group chat is ours."""
    closed = roster is None or await roster.has_outsiders(inbound.channel_id)
    return dataclasses.replace(inbound, is_external=closed, is_external_known=not closed)

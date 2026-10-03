"""Who in a Teams conversation is answered as from another organisation.

Two kinds of people: a shared channel's external participants (B2B direct
connect), who stay in their home tenant, and guests, whose account lives in
ours. Signals, cheapest first; the first that decides wins:

1. a foreign tenant on the activity (`identity.foreign_tenant`);
2. the sender's Bot Framework roster entry: `tenantId`, and `userRole` "guest";
3. in a channel, Graph's member list (`allMembers`): `tenantId`, and a "guest" role.

A foreign tenant is external. A guest is too, unless the tenant's access
policy lists them (`member_guest_ids`); the list never overrides a foreign
tenant. Internal takes positive evidence: our tenant and a member's role.
Anything else is unknown (`is_known=False`, never stored): in a channel not
known to be standard or private it is answered as external for that turn;
in those, and in a 1:1 chat, as internal unless stored evidence says
otherwise (admission). A channel's type comes from the activity, else an
earlier activity there, else the team's channel list. Answers are cached
briefly, every lookup is bounded, and `classify` never raises.

Each kind can be unrestricted (`restrict_guests`,
`restrict_external_participants`): its people are then members, known, so
admission clears any stored flag, and the lookups only it needs are skipped.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

import httpx
import structlog
from daimon.adapters.teams.identity import canonical_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.teams_graph import GraphUnavailable

log = structlog.get_logger()


@dataclass(frozen=True)
class MemberFacts:
    """What a member list says about one person: home tenant, and whether a guest
    (None when it gives no role)."""

    tenant_id: str | None = None
    is_guest: bool | None = None


# (conversation id, Entra object id) -> their roster entry, None when not on it. Raises on failure.
MemberFetch = Callable[[str, str], Awaitable[MemberFacts | None]]
# (team id, channel id) -> the channel's type, None when the team does not list it.
ChannelTypeFetch = Callable[[str, str], Awaitable[str | None]]
# (team id, team group id, channel id) -> its members by lower-case Entra object id.
ChannelMembersFetch = Callable[[str | None, str | None, str], Awaitable[Mapping[str, MemberFacts]]]
# The guests the tenant treats as members (`member_guest_ids`).
MemberGuestsFetch = Callable[[], Awaitable[frozenset[str]]]

LOOKUP_ERRORS: Final = (
    httpx.HTTPError,
    OSError,
    ValueError,
    TimeoutError,
    GraphUnavailable,
    AccessPolicyUnreadable,
)
LOOKUP_TIMEOUT_S: Final = 3.0
MEMBERS_TIMEOUT_S: Final = 5.0
INTERNAL_TTL_S: Final = 600.0
EXTERNAL_TTL_S: Final = 3600.0
FAILURE_TTL_S: Final = 60.0
# A 403 is a missing consent, which only a re-upload fixes: ask again rarely.
NO_CONSENT_TTL_S: Final = 600.0
MEMBERS_TTL_S: Final = 300.0
CHANNEL_TTL_S: Final = 600.0
CONTEXT_TTL_S: Final = 86400.0
MAX_ENTRIES: Final = 4096

_Failed = Literal["failed"]
_FAILED: Final[_Failed] = "failed"


@dataclass(frozen=True)
class Membership:
    """How to treat a sender: `is_external` this turn, `is_known` on positive evidence."""

    is_external: bool
    is_known: bool
    home_tenant_id: str | None = None
    is_guest: bool = False


INTERNAL: Final = Membership(is_external=False, is_known=True)


@dataclass(frozen=True)
class _ChannelContext:
    """What earlier activities in a channel said, for invokes and queued work that don't."""

    channel_type: str | None = None
    team_id: str | None = None
    team_group_id: str | None = None


def _shared(channel_type: str) -> bool | None:
    """Only the types known not to admit other organisations count as not shared:
    Microsoft may add or rename types, and a new one is not assumed safe."""
    kind = channel_type.strip().lower()
    return False if kind in ("standard", "private") else True if kind == "shared" else None


class _TtlCache[K, V]:
    """A small bounded map whose entries expire; the oldest goes first when full."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._entries: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def get(self, key: K) -> tuple[V] | None:
        entry = self._entries.get(key)
        if entry is None or self._clock() >= entry[1]:
            self._entries.pop(key, None)
            return None
        return (entry[0],)

    def put(self, key: K, value: V, ttl_s: float) -> None:
        self._entries[key] = (value, self._clock() + ttl_s)
        self._entries.move_to_end(key)
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)


class ExternalParticipants:
    """Classifies a Teams sender as one of ours or from another organisation."""

    def __init__(
        self,
        *,
        tenant_id: str,
        roster_member: MemberFetch,
        channel_type: ChannelTypeFetch,
        channel_members: ChannelMembersFetch,
        member_guests: MemberGuestsFetch,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = LOOKUP_TIMEOUT_S,
        members_timeout_s: float = MEMBERS_TIMEOUT_S,
        restrict_guests: bool = True,
        restrict_external: bool = True,
    ) -> None:
        self._restrict_guests = restrict_guests
        self._restrict_external = restrict_external
        self._tenant_id = canonical_uuid(tenant_id) or tenant_id
        self._roster_member = roster_member
        self._channel_type = channel_type
        self._channel_members = channel_members
        self._member_guests = member_guests
        self._timeout_s = timeout_s
        self._members_timeout_s = members_timeout_s
        # Roster facts are the person's own, so they hold in every conversation.
        self._people = _TtlCache[str, MemberFacts](clock)
        self._misses = _TtlCache[tuple[str, str], bool](clock)
        self._members = _TtlCache[tuple[str, str], Mapping[str, MemberFacts] | _Failed](clock)
        self._channels = _TtlCache[tuple[str, str], str | None](clock)
        self._contexts = _TtlCache[str, _ChannelContext](clock)

    async def is_shared(
        self, channel_id: str, *, team_id: str | None, reported_type: str | None
    ) -> bool | None:
        """Whether the channel is shared; None when neither an activity nor its team says."""
        if reported_type is not None:
            return _shared(reported_type)
        if team_id is None:
            return None
        key = (team_id, channel_id)
        cached = self._channels.get(key)
        if cached is None:
            try:
                kind = await asyncio.wait_for(
                    self._channel_type(team_id, channel_id), self._timeout_s
                )
            except LOOKUP_ERRORS as err:
                log.info("teams.channel_type.lookup_failed", error=type(err).__name__)
                return None
            self._channels.put(key, kind, CHANNEL_TTL_S)
            cached = (kind,)
        return None if cached[0] is None else _shared(cached[0])

    async def classify(
        self,
        *,
        kind: Literal["channel", "dm"],
        conversation_id: str,
        user_id: str,
        team_id: str | None = None,
        team_group_id: str | None = None,
        channel_type: str | None = None,
        foreign_tenant: str | None = None,
    ) -> Membership:
        """The sender's membership (module docstring); never raises.

        `conversation_id` is a channel's id (no `;messageid=`) or the 1:1 chat's.
        """
        shared: bool | None = False if kind == "dm" or not self._restrict_external else None
        try:
            if not (self._restrict_guests or self._restrict_external):
                return INTERNAL
            if foreign_tenant is not None:
                return self._foreign(foreign_tenant)
            context = _ChannelContext()
            if kind == "channel":
                context = self._remember(conversation_id, channel_type, team_id, team_group_id)
                if self._restrict_external:
                    shared = await self.is_shared(
                        conversation_id, team_id=context.team_id, reported_type=context.channel_type
                    )
            if shared is False and not self._restrict_guests and self._restrict_external:
                # Outside shared channels everyone is in our tenant: only guests could be held.
                return INTERNAL
            verdict = self._judge(await self._roster(conversation_id, user_id))
            if verdict is None and kind == "channel":
                verdict = self._judge(await self._listed(context, conversation_id, user_id))
            if verdict is not None:
                return await self._settle(verdict, user_id)
        # The boundary of every lookup: a sender we can't place is unknown, never an error.
        except Exception as err:
            log.warning("teams.external.classify_failed", error=type(err).__name__)
        if shared is not False:
            log.info("teams.external.fail_closed", shared=shared)
        return Membership(is_external=shared is not False, is_known=False)

    def _remember(
        self,
        channel_id: str,
        channel_type: str | None,
        team_id: str | None,
        team_group_id: str | None,
    ) -> _ChannelContext:
        cached = self._contexts.get(channel_id)
        known = cached[0] if cached is not None else _ChannelContext()
        merged = _ChannelContext(
            channel_type=channel_type or known.channel_type,
            team_id=team_id or known.team_id,
            team_group_id=team_group_id or known.team_group_id,
        )
        if merged != known:
            self._contexts.put(channel_id, merged, CONTEXT_TTL_S)
        return merged

    def _judge(self, facts: MemberFacts | None) -> Membership | None:
        """What one entry decides, or None when it decides nothing."""
        if facts is None:
            return None
        tenant = canonical_uuid(facts.tenant_id) or facts.tenant_id
        if tenant and tenant != self._tenant_id:
            return self._foreign(tenant)
        if facts.is_guest and self._restrict_guests:
            return Membership(is_external=True, is_known=True, is_guest=True)
        if facts.is_guest or (tenant == self._tenant_id and not self._restrict_guests):
            return INTERNAL
        if tenant == self._tenant_id and facts.is_guest is False:
            return INTERNAL
        return None

    def _foreign(self, tenant: str) -> Membership:
        return Membership(is_external=self._restrict_external, is_known=True, home_tenant_id=tenant)

    async def _settle(self, verdict: Membership, user_id: str) -> Membership:
        """A guest the tenant lists as a member is internal; an unreadable list keeps
        them external, for this turn only."""
        if not verdict.is_guest:
            return verdict
        try:
            listed = await asyncio.wait_for(self._member_guests(), self._timeout_s)
        except LOOKUP_ERRORS as err:
            log.info("teams.member_guests.lookup_failed", error=type(err).__name__)
            return Membership(is_external=True, is_known=False, is_guest=True)
        return INTERNAL if user_id.lower() in listed else verdict

    async def _roster(self, conversation_id: str, user_id: str) -> MemberFacts | None:
        if (known := self._people.get(user_id)) is not None:
            return known[0]
        key = (conversation_id, user_id)
        if self._misses.get(key) is not None:
            return None
        try:
            facts = await asyncio.wait_for(
                self._roster_member(conversation_id, user_id), self._timeout_s
            )
        except LOOKUP_ERRORS as err:
            log.info("teams.roster.lookup_failed", error=type(err).__name__)
            facts = None
        if facts is None:
            self._misses.put(key, True, FAILURE_TTL_S)
            return None
        self._people.put(user_id, facts, self._ttl(facts))
        return facts

    async def _listed(
        self, context: _ChannelContext, channel_id: str, user_id: str
    ) -> MemberFacts | None:
        """The sender's entry in the channel's Graph member list, read once per channel."""
        team = context.team_group_id or context.team_id
        if team is None:
            return None
        key = (team, channel_id)
        cached = self._members.get(key)
        if cached is None:
            try:
                members = await asyncio.wait_for(
                    self._channel_members(context.team_id, context.team_group_id, channel_id),
                    self._members_timeout_s,
                )
            except LOOKUP_ERRORS as err:
                no_consent = isinstance(err, GraphUnavailable) and err.status == 403
                log.info("teams.channel_members.lookup_failed", error=type(err).__name__)
                self._members.put(key, _FAILED, NO_CONSENT_TTL_S if no_consent else FAILURE_TTL_S)
                return None
            self._members.put(key, members, MEMBERS_TTL_S)
            cached = (members,)
        members = cached[0]
        return None if members == _FAILED else members.get(user_id.lower())

    def _ttl(self, facts: MemberFacts) -> float:
        verdict = self._judge(facts)
        if verdict is None:
            return FAILURE_TTL_S
        return EXTERNAL_TTL_S if verdict.is_external else INTERNAL_TTL_S

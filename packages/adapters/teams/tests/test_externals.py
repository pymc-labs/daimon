"""ExternalParticipants: which signal places a sender, in what order, and how long it holds."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Mapping

import httpx
import pytest
from daimon.adapters.teams.externals import (
    EXTERNAL_TTL_S,
    FAILURE_TTL_S,
    INTERNAL,
    INTERNAL_TTL_S,
    NO_CONSENT_TTL_S,
    ExternalParticipants,
    MemberFacts,
    Membership,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.teams_graph import GraphUnavailable

from .conftest import AAD_OBJECT_ID, CHANNEL_ID, ENTRA_TENANT_ID

OTHER_TENANT = "99999999-9999-4999-8999-999999999999"
TEAM = "19:team@thread.tacv2"
GROUP = "11111111-1111-4111-8111-111111111111"
DM = "a:1dm"
UNDECIDED = MemberFacts(tenant_id=ENTRA_TENANT_ID)  # our tenant, no role: could be a guest
MEMBER = MemberFacts(tenant_id=ENTRA_TENANT_ID, is_guest=False)
GUEST = MemberFacts(tenant_id=ENTRA_TENANT_ID, is_guest=True)
FOREIGN = MemberFacts(tenant_id=OTHER_TENANT, is_guest=False)
EXTERNAL = Membership(is_external=True, is_known=True, home_tenant_id=OTHER_TENANT)
HELD_GUEST = Membership(is_external=True, is_known=True, is_guest=True)
UNKNOWN_HELD = Membership(is_external=True, is_known=False)
UNKNOWN_OPEN = Membership(is_external=False, is_known=False)

type _Answer[T] = T | Exception


@dataclasses.dataclass
class _Lookups:
    """Each lookup's answer; an exception is raised, and every call is counted."""

    roster: _Answer[MemberFacts | None] = MEMBER
    channel_type: _Answer[str | None] = None
    members: _Answer[Mapping[str, MemberFacts]] = dataclasses.field(default_factory=dict)
    guests: _Answer[frozenset[str]] = frozenset()
    hang: bool = False
    calls: list[str] = dataclasses.field(default_factory=list)

    @staticmethod
    def _give[T](answer: _Answer[T]) -> T:
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def roster_member(self, conversation_id: str, aad_object_id: str) -> MemberFacts | None:
        assert aad_object_id == AAD_OBJECT_ID
        self.calls.append(f"roster:{conversation_id}")
        if self.hang:
            await asyncio.sleep(10)
        return self._give(self.roster)

    async def channel_kind(self, team_id: str, channel_id: str) -> str | None:
        assert (team_id, channel_id) == (TEAM, CHANNEL_ID)
        self.calls.append("type")
        return self._give(self.channel_type)

    async def channel_members(
        self, team_id: str | None, team_group_id: str | None, channel_id: str
    ) -> Mapping[str, MemberFacts]:
        assert channel_id == CHANNEL_ID
        self.calls.append(f"members:{team_id}:{team_group_id}")
        return self._give(self.members)

    async def member_guests(self) -> frozenset[str]:
        self.calls.append("guests")
        return self._give(self.guests)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _detector(
    lookups: _Lookups,
    clock: _Clock | None = None,
    *,
    restrict_guests: bool = True,
    restrict_external: bool = True,
) -> ExternalParticipants:
    return ExternalParticipants(
        tenant_id=ENTRA_TENANT_ID,
        roster_member=lookups.roster_member,
        channel_type=lookups.channel_kind,
        channel_members=lookups.channel_members,
        member_guests=lookups.member_guests,
        clock=clock or _Clock(),
        timeout_s=0.05,
        members_timeout_s=0.05,
        restrict_guests=restrict_guests,
        restrict_external=restrict_external,
    )


async def _channel(
    detector: ExternalParticipants,
    *,
    channel_type: str | None = "shared",
    team_id: str | None = TEAM,
    team_group_id: str | None = None,
    foreign_tenant: str | None = None,
) -> Membership:
    return await detector.classify(
        kind="channel",
        conversation_id=CHANNEL_ID,
        user_id=AAD_OBJECT_ID,
        team_id=team_id,
        team_group_id=team_group_id,
        channel_type=channel_type,
        foreign_tenant=foreign_tenant,
    )


async def _dm(detector: ExternalParticipants) -> Membership:
    return await detector.classify(kind="dm", conversation_id=DM, user_id=AAD_OBJECT_ID)


async def test_a_foreign_tenant_on_the_activity_decides_before_any_lookup() -> None:
    lookups = _Lookups()
    membership = await _channel(_detector(lookups), foreign_tenant=OTHER_TENANT)
    assert membership == EXTERNAL
    assert lookups.calls == []


@pytest.mark.parametrize("channel_type", ["shared", "standard", "private"])
async def test_the_roster_decides_a_member_and_a_foreign_tenant(channel_type: str) -> None:
    assert await _channel(_detector(_Lookups()), channel_type=channel_type) == INTERNAL
    assert await _channel(_detector(_Lookups(roster=FOREIGN)), channel_type=channel_type) == (
        EXTERNAL
    )


async def test_a_guest_by_the_roster_is_external_in_a_standard_channel_and_a_1on1() -> None:
    lookups = _Lookups(roster=GUEST)
    assert await _channel(_detector(lookups), channel_type="standard") == HELD_GUEST
    assert await _dm(_detector(lookups)) == HELD_GUEST
    assert "members:" not in " ".join(lookups.calls), "the roster decided"


async def test_the_member_list_decides_when_the_roster_cannot() -> None:
    members = {AAD_OBJECT_ID: GUEST}
    lookups = _Lookups(roster=UNDECIDED, members=members)
    membership = await _channel(_detector(lookups), channel_type="private", team_group_id=GROUP)
    assert membership == HELD_GUEST
    assert lookups.calls == [f"roster:{CHANNEL_ID}", f"members:{TEAM}:{GROUP}", "guests"]


async def test_the_member_list_carries_a_foreign_tenant() -> None:
    lookups = _Lookups(roster=None, members={AAD_OBJECT_ID: FOREIGN})
    assert await _channel(_detector(lookups)) == EXTERNAL


async def test_a_listed_member_guest_is_internal() -> None:
    lookups = _Lookups(roster=GUEST, guests=frozenset({AAD_OBJECT_ID}))
    assert await _channel(_detector(lookups), channel_type="standard") == INTERNAL
    assert await _dm(_detector(lookups)) == INTERNAL


async def test_the_member_guest_list_never_overrides_a_foreign_tenant() -> None:
    lookups = _Lookups(roster=FOREIGN, guests=frozenset({AAD_OBJECT_ID}))
    assert await _channel(_detector(lookups)) == EXTERNAL
    assert await _channel(_detector(lookups), foreign_tenant=OTHER_TENANT) == EXTERNAL
    assert "guests" not in lookups.calls


async def test_an_unreadable_member_guest_list_holds_the_guest_for_the_turn_only() -> None:
    lookups = _Lookups(roster=GUEST, guests=AccessPolicyUnreadable(tenant_id=uuid.uuid4()))
    membership = await _channel(_detector(lookups), channel_type="standard")
    assert membership == Membership(is_external=True, is_known=False, is_guest=True)


@pytest.mark.parametrize(
    ("channel_type", "expected"),
    [
        ("shared", UNKNOWN_HELD),
        (None, UNKNOWN_HELD),
        ("sharedChannel", UNKNOWN_HELD),  # a type Teams may add later is not assumed safe
        ("standard", UNKNOWN_OPEN),
        ("Private", UNKNOWN_OPEN),
    ],
)
async def test_no_evidence_is_unknown_held_only_outside_known_standard_channels(
    channel_type: str | None, expected: Membership
) -> None:
    lookups = _Lookups(roster=None, members=GraphUnavailable("http error", status=500))
    detector = _detector(lookups)
    assert await _channel(detector, channel_type=channel_type, team_id=None) == expected


async def test_no_evidence_in_a_1on1_is_unknown_and_open() -> None:
    assert await _dm(_detector(_Lookups(roster=httpx.ConnectError("down")))) == UNKNOWN_OPEN


async def test_a_hanging_roster_is_bounded() -> None:
    lookups = _Lookups(hang=True)
    assert await _channel(_detector(lookups), team_id=None) == UNKNOWN_HELD


async def test_classify_never_raises() -> None:
    lookups = _Lookups(roster=RuntimeError("bug"))
    assert await _channel(_detector(lookups)) == UNKNOWN_HELD
    assert await _dm(_detector(lookups)) == UNKNOWN_OPEN


async def test_a_channel_type_from_an_earlier_activity_serves_an_invoke_without_one() -> None:
    lookups = _Lookups(roster=None, members={})
    detector = _detector(lookups)
    await _channel(detector, channel_type="standard")
    lookups.calls.clear()
    membership = await _channel(detector, channel_type=None, team_id=None)
    assert membership == UNKNOWN_OPEN, "remembered as standard, with its team"
    assert "type" not in lookups.calls


async def test_the_teams_channel_list_names_the_type_once() -> None:
    lookups = _Lookups(roster=None, channel_type="standard")
    detector = _detector(lookups)
    assert await _channel(detector, channel_type=None) == UNKNOWN_OPEN
    assert await _channel(detector, channel_type=None) == UNKNOWN_OPEN
    assert lookups.calls.count("type") == 1


async def test_answers_are_cached_per_person_for_their_ttl() -> None:
    clock = _Clock()
    lookups = _Lookups(roster=MEMBER)
    detector = _detector(lookups, clock)
    await _channel(detector)
    await _dm(detector)
    assert len([c for c in lookups.calls if c.startswith("roster")]) == 1, "one person, one ask"
    clock.now += INTERNAL_TTL_S
    await _dm(detector)
    assert len([c for c in lookups.calls if c.startswith("roster")]) == 2


async def test_an_external_answer_holds_longer() -> None:
    clock = _Clock()
    lookups = _Lookups(roster=FOREIGN)
    detector = _detector(lookups, clock)
    await _channel(detector)
    clock.now += INTERNAL_TTL_S
    assert await _channel(detector) == EXTERNAL
    assert len(lookups.calls) == 1
    clock.now += EXTERNAL_TTL_S
    await _channel(detector)
    assert len(lookups.calls) == 2


async def test_a_failure_is_retried_after_a_minute_and_a_missing_consent_after_longer() -> None:
    clock = _Clock()
    lookups = _Lookups(roster=None, members=GraphUnavailable("http error", status=403))
    detector = _detector(lookups, clock)
    await _channel(detector)
    clock.now += FAILURE_TTL_S
    await _channel(detector)
    assert lookups.calls.count(f"members:{TEAM}:None") == 1, "a 403 is asked again rarely"
    assert lookups.calls.count(f"roster:{CHANNEL_ID}") == 2
    clock.now += NO_CONSENT_TTL_S
    await _channel(detector)
    assert lookups.calls.count(f"members:{TEAM}:None") == 2


async def test_unrestricted_guests_are_members_while_externals_stay_held() -> None:
    lookups = _Lookups(roster=GUEST)
    detector = _detector(lookups, restrict_guests=False)
    assert await _channel(detector, channel_type="standard") == INTERNAL
    assert await _dm(detector) == INTERNAL
    assert lookups.calls == [], "outside shared channels only guests could be held"
    assert await _channel(detector) == INTERNAL
    foreign = _detector(_Lookups(roster=FOREIGN), restrict_guests=False)
    assert await _channel(foreign) == EXTERNAL


async def test_unrestricted_externals_are_members_while_guests_stay_held() -> None:
    lookups = _Lookups(roster=FOREIGN)
    detector = _detector(lookups, restrict_external=False)
    member = Membership(is_external=False, is_known=True, home_tenant_id=OTHER_TENANT)
    assert await _channel(detector) == member
    assert await _channel(detector, foreign_tenant=OTHER_TENANT) == member
    assert "type" not in lookups.calls
    assert await _channel(_detector(_Lookups(roster=None), restrict_external=False)) == (
        UNKNOWN_OPEN
    )
    guest = _detector(_Lookups(roster=GUEST), restrict_external=False)
    assert await _channel(guest, channel_type="standard") == HELD_GUEST


async def test_with_both_unrestricted_nothing_is_looked_up() -> None:
    lookups = _Lookups(roster=GUEST)
    detector = _detector(lookups, restrict_guests=False, restrict_external=False)
    assert await _channel(detector, foreign_tenant=OTHER_TENANT) == INTERNAL
    assert await _dm(detector) == INTERNAL
    assert lookups.calls == []

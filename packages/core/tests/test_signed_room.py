"""The signed `room` claim: a room's kind from its facts, and only on Discord with a channel."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.mcp_identity import TurnPlace, caller_claims
from daimon.core.signed_room import SignedRoom, room_of, turn_room
from daimon.core.stores.domain import Role, TurnOriginRow

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("facts", "room"),
    [
        (RoomFacts(contained=False, narrow=False), "team"),
        (RoomFacts(contained=False, narrow=True), "narrow"),
        (RoomFacts(contained=True, narrow=False), "contained"),
        (RoomFacts(contained=True, narrow=True), "contained"),
        (UNKNOWN_FACTS, "contained"),
    ],
)
def test_room_of_puts_contained_and_unknown_first(facts: RoomFacts, room: str) -> None:
    assert room_of(facts) == room


async def test_a_turn_off_discord_has_no_room() -> None:
    origin = TurnOriginRow(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), platform="slack",
        parent_channel_id="C1", thread_id="C1", responder_ma_agent_id="agent_a",
        responder_name="Helper", configuration_target_ma_agent_id=None,
        configuration_target_name=None, role=Role.USER, created_at=NOW, expires_at=NOW,
    )  # fmt: skip
    # No database is reached: the sessionmaker would fail if it were called.
    assert await turn_room(None, origin, now=NOW) is None  # pyright: ignore[reportArgumentType]


def test_a_room_is_signed_only_with_a_channel() -> None:
    def claims(place: TurnPlace | None) -> dict[str, str | int]:
        return caller_claims(
            issuer="daimon-test", audience="https://tools.example.com", agent_id="agent-1",
            platform="discord", workspace_id="1", platform_user_id="2", agent_name="Helper",
            place=place, room="team",
        ).payload(now=NOW)  # fmt: skip

    assert claims(TurnPlace("3", "4"))["room"] == "team", "a place names its room"
    assert "room" not in claims(None), "no place, no room"
    assert "room" not in claims(TurnPlace(None, None)), "no channel, no room"


@pytest.mark.parametrize(
    ("place", "room", "routine"),
    [
        (TurnPlace("3", None), None, "r-1"),
        (TurnPlace("3", None), "dm", "r-1"),
        (None, "team", "r-1"),
        (TurnPlace("3", None), "team", ""),
        (TurnPlace("3", None), "team", "r" * 81),
    ],
    ids=["no-room", "dm", "no-channel", "blank", "too-long"],
)
def test_a_routine_claim_is_never_signed_without_its_room(
    place: TurnPlace | None, room: SignedRoom | None, routine: str
) -> None:
    with pytest.raises(ValueError):
        caller_claims(
            issuer="daimon-test", audience="https://tools.example.com", agent_id="agent-1",
            platform="discord", workspace_id="1", platform_user_id="2", agent_name="Helper",
            place=place, room=room, routine=routine,
        )  # fmt: skip


def test_a_routine_claim_names_no_person_even_when_its_credential_does() -> None:
    claims = caller_claims(
        issuer="daimon-test", audience="https://tools.example.com", agent_id="agent-1",
        platform="discord", workspace_id="1", platform_user_id="2", agent_name="Helper",
        place=TurnPlace("3", "4"), room="team", routine="r-1",
    ).payload(now=NOW)  # fmt: skip
    assert (claims["routine"], claims["channel"], claims["room"]) == ("r-1", "3", "team")
    assert not {"sub", "msg"} & set(claims), "the owner's account is not the asker"

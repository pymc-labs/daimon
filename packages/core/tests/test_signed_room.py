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


def _wake(place: TurnPlace | None, room: SignedRoom | None, wake: str, routine: str | None = None):
    return caller_claims(
        issuer="daimon-test", audience="https://tools.example.com", agent_id="agent-1",
        platform="discord", workspace_id="1", platform_user_id="2", agent_name="Helper",
        place=place, room=room, routine=routine, wake=wake,  # pyright: ignore[reportArgumentType]
    )  # fmt: skip


@pytest.mark.parametrize(
    ("place", "room", "wake", "routine"),
    [
        (TurnPlace("3", None), "team", "timer", "r-1"),
        (TurnPlace("3", "4"), "team", "turn", "r-1"),
        (TurnPlace("3", None), "team", "routine", None),
        (TurnPlace("3", None), "team", "", None),
        (TurnPlace("3", None), "team", "turn", None),
        (None, None, "turn", None),
        (TurnPlace("3", "4"), "team", "timer", None),
        (TurnPlace("3", None), None, "timer", None),
        (TurnPlace("3", None), "dm", "timer", None),
        (None, "team", "timer", None),
    ],
    ids=[
        "with-routine", "turn-with-routine", "unknown", "blank", "turn-without-msg",
        "turn-without-place", "reason-with-msg", "reason-without-room", "reason-in-dm",
        "reason-without-channel",
    ],
)  # fmt: skip
def test_a_wake_claim_is_refused_in_any_mix_a_server_would_refuse(
    place: TurnPlace | None, room: SignedRoom | None, wake: str, routine: str | None
) -> None:
    with pytest.raises(ValueError):
        _wake(place, room, wake, routine)


def test_a_turn_wake_keeps_the_person_and_a_continuation_wake_names_none() -> None:
    turn = _wake(TurnPlace("3", "4"), "team", "turn").payload(now=NOW)
    assert (turn["wake"], turn["sub"], turn["msg"]) == ("turn", "discord:2", "4")
    reason = _wake(TurnPlace("3", None), "narrow", "watch_report").payload(now=NOW)
    assert (reason["wake"], reason["channel"], reason["room"]) == ("watch_report", "3", "narrow")
    assert not {"sub", "msg", "routine"} & set(reason), "the requester's account is not the asker"

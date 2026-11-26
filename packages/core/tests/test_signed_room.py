"""The signed `room` claim: a room's kind from its facts, and only on Discord with a channel."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.mcp_identity import TurnPlace, caller_claims
from daimon.core.signed_room import room_of, turn_room
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

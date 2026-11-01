"""A workspace's member list follows Slack: users.list at start, then team_join and user_change."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.member_sync import SlackMemberSync, member_entry
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T01"


def _user(user_id: str, **flags: object) -> dict[str, object]:
    return {"id": user_id, "team_id": _TEAM, **flags}


def test_only_people_of_the_workspace_are_members() -> None:
    entries = [
        member_entry(u, team_id=_TEAM)
        for u in (
            _user("U1", is_owner=True),
            _user("U2"),
            _user("U3", is_bot=True),
            _user("U4", deleted=True),
            _user("USLACKBOT"),
            {"id": "U5", "team_id": "T_OTHER"},
        )
    ]

    assert entries == [("U1", True), ("U2", False), None, None, None, None]


def _client() -> Any:
    client = MagicMock()
    client.auth_test = AsyncMock(return_value={"team": "Team"})
    client.users_list = AsyncMock(
        side_effect=[
            {"members": [_user("U1", is_admin=True)], "response_metadata": {"next_cursor": "c"}},
            {"members": [_user("U2"), _user("B1", is_bot=True)], "response_metadata": {}},
        ]
    )
    return client


async def _rows(factory: async_sessionmaker[AsyncSession]) -> dict[str, bool]:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT platform_user_id, is_admin FROM workspace_members")
        )
        return {user_id: is_admin for user_id, is_admin in rows.tuples()}


async def test_load_pages_users_list_then_events_keep_it_current(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await db_session.commit()
    runtime = MagicMock(sessionmaker=db_session_factory)
    sync = SlackMemberSync(runtime, clock=lambda: datetime(2026, 5, 1, tzinfo=UTC))
    with patch(
        "daimon.adapters.slack.member_sync.resolve_web_client", AsyncMock(return_value=_client())
    ):
        assert sync.claim_load(_TEAM) and not sync.claim_load(_TEAM), "one load at a time"
        await sync.load(_TEAM)

    assert await _rows(db_session_factory) == {"U1": True, "U2": False}

    await sync.user_event(_TEAM, _user("U3"))
    await sync.user_event(_TEAM, _user("U2", deleted=True))
    await sync.user_event(_TEAM, _user("U1"))
    assert await _rows(db_session_factory) == {"U1": False, "U3": False}, (
        "a join adds, a deactivation removes, a demotion clears admin"
    )


async def test_a_workspace_not_installed_is_tried_again_later() -> None:
    clock = [0.0]
    sync = SlackMemberSync(MagicMock(), monotonic=lambda: clock[0])
    with patch(
        "daimon.adapters.slack.member_sync.resolve_web_client", AsyncMock(return_value=None)
    ):
        assert sync.claim_load(_TEAM)
        await sync.load(_TEAM)

    assert not sync.claim_load(_TEAM), "not at once"
    clock[0] += 61
    assert sync.claim_load(_TEAM), "but after a minute"

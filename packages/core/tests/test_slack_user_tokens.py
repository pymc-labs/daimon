"""Real-Postgres round-trip tests for the slack_user_tokens store."""

from __future__ import annotations

from daimon.core.stores.slack_user_tokens import (
    delete_slack_user_token,
    get_slack_user_token,
    upsert_slack_user_token,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def test_upsert_then_get_round_trips_token(db_session: AsyncSession) -> None:
    await upsert_slack_user_token(
        db_session,
        team_id="T1",
        slack_user_id="U1",
        encrypted_token=b"ct-1",
        scopes="search:read",
    )
    row = await get_slack_user_token(db_session, team_id="T1", slack_user_id="U1")
    assert row is not None, "stored row must be retrievable by (team_id, slack_user_id)"
    assert row.encrypted_token == b"ct-1", "ciphertext must round-trip untouched"
    assert row.scopes == "search:read", "granted scopes must round-trip"


async def test_upsert_replaces_token_on_reconnect(db_session: AsyncSession) -> None:
    await upsert_slack_user_token(
        db_session, team_id="T1", slack_user_id="U1", encrypted_token=b"old", scopes="a"
    )
    await upsert_slack_user_token(
        db_session, team_id="T1", slack_user_id="U1", encrypted_token=b"new", scopes="a,b"
    )
    row = await get_slack_user_token(db_session, team_id="T1", slack_user_id="U1")
    assert row is not None and row.encrypted_token == b"new", (
        "re-connect must replace the stored ciphertext (upsert on composite PK)"
    )
    assert row.scopes == "a,b", "re-connect must replace the stored scope set"


async def test_get_is_scoped_per_user(db_session: AsyncSession) -> None:
    await upsert_slack_user_token(
        db_session, team_id="T1", slack_user_id="U1", encrypted_token=b"ct", scopes="a"
    )
    assert await get_slack_user_token(db_session, team_id="T1", slack_user_id="U2") is None, (
        "another user in the same workspace must not see U1's token"
    )
    assert await get_slack_user_token(db_session, team_id="T2", slack_user_id="U1") is None, (
        "the same user id in another workspace must not match"
    )


async def test_delete_removes_row_and_is_idempotent(db_session: AsyncSession) -> None:
    await upsert_slack_user_token(
        db_session, team_id="T1", slack_user_id="U1", encrypted_token=b"ct", scopes="a"
    )
    assert await delete_slack_user_token(db_session, team_id="T1", slack_user_id="U1") == 1
    assert await get_slack_user_token(db_session, team_id="T1", slack_user_id="U1") is None
    assert await delete_slack_user_token(db_session, team_id="T1", slack_user_id="U1") == 0, (
        "second delete must be a no-op returning rowcount 0"
    )

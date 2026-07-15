"""Tests for slack_bot_tokens store rotation fields (Phase 79, SINST-04).

Verifies that migration 0027's new nullable columns (expires_at, refresh_token)
round-trip through upsert_slack_bot_token + get_slack_bot_token, and that a
re-upsert updates those fields in the on_conflict set_.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.stores.slack_bot_tokens import (
    get_slack_bot_token,
    upsert_slack_bot_token,
)
from sqlalchemy.ext.asyncio import AsyncSession

_TEAM_ID = "T_ROTATION_01"
_ENC_TOKEN = b"fernet-encrypted-xoxb-bytes"
_ENC_REFRESH = b"fernet-encrypted-xoxe-bytes"
_EXPIRES_AT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


async def test_upsert_slack_bot_token_round_trips_rotation_fields(
    db_session: AsyncSession,
) -> None:
    """upsert_slack_bot_token stores and returns expires_at + refresh_token."""
    row = await upsert_slack_bot_token(
        db_session,
        team_id=_TEAM_ID,
        encrypted_token=_ENC_TOKEN,
        expires_at=_EXPIRES_AT,
        refresh_token=_ENC_REFRESH,
    )

    assert row.expires_at == _EXPIRES_AT, (
        "expires_at should round-trip through upsert_slack_bot_token"
    )
    assert row.refresh_token == _ENC_REFRESH, (
        "refresh_token should round-trip through upsert_slack_bot_token"
    )

    fetched = await get_slack_bot_token(db_session, team_id=_TEAM_ID)
    assert fetched is not None, "get_slack_bot_token must return the row after upsert"
    assert fetched.expires_at == _EXPIRES_AT, (
        "expires_at must be preserved on get_slack_bot_token re-read"
    )
    assert fetched.refresh_token == _ENC_REFRESH, (
        "refresh_token must be preserved on get_slack_bot_token re-read"
    )


async def test_upsert_slack_bot_token_re_upsert_updates_rotation_fields(
    db_session: AsyncSession,
) -> None:
    """A second upsert with new rotation values updates the on_conflict set_ columns."""
    team_id = "T_ROTATION_02"
    initial_expires = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    updated_expires = initial_expires + timedelta(hours=12)

    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=b"first-token",
        expires_at=initial_expires,
        refresh_token=b"first-refresh",
    )

    re_upserted = await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=b"second-token",
        expires_at=updated_expires,
        refresh_token=b"second-refresh",
    )

    assert re_upserted.encrypted_token == b"second-token", "re-upsert must replace encrypted_token"
    assert re_upserted.expires_at == updated_expires, (
        "re-upsert must replace expires_at via on_conflict set_"
    )
    assert re_upserted.refresh_token == b"second-refresh", (
        "re-upsert must replace refresh_token via on_conflict set_"
    )


async def test_upsert_slack_bot_token_without_rotation_fields_sets_none(
    db_session: AsyncSession,
) -> None:
    """Calling upsert without rotation kwargs leaves both fields as None."""
    row = await upsert_slack_bot_token(
        db_session,
        team_id="T_NO_ROTATION",
        encrypted_token=b"plain-token",
    )

    assert row.expires_at is None, (
        "expires_at should be None when not provided (rotation not enabled)"
    )
    assert row.refresh_token is None, (
        "refresh_token should be None when not provided (rotation not enabled)"
    )

    fetched = await get_slack_bot_token(db_session, team_id="T_NO_ROTATION")
    assert fetched is not None, "row must exist after upsert"
    assert fetched.expires_at is None, "expires_at must remain None on re-read"
    assert fetched.refresh_token is None, "refresh_token must remain None on re-read"

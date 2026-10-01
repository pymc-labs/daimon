"""Async store for slack_bot_tokens.

UPSERT on team_id: installing a workspace bot re-OAuth replaces
the row with a fresh encrypted_token and bumps updated_at. No try/except —
DB exceptions propagate to the adapter boundary.

The store is pure ciphertext-bytes in/out; it never sees the Fernet key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import SlackBotToken
from daimon.core.stores.domain import SlackBotTokenRow
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_slack_bot_token(
    session: AsyncSession,
    *,
    team_id: str,
    encrypted_token: bytes,
    expires_at: datetime | None = None,
    refresh_token: bytes | None = None,
) -> SlackBotTokenRow:
    now = datetime.now(tz=UTC)
    stmt = pg_insert(SlackBotToken).values(
        team_id=team_id,
        encrypted_token=encrypted_token,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        refresh_token=refresh_token,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SlackBotToken.team_id],
        set_={
            "encrypted_token": encrypted_token,
            "updated_at": now,
            "expires_at": expires_at,
            "refresh_token": refresh_token,
        },
    ).returning(SlackBotToken)

    result = await session.execute(stmt)
    return SlackBotTokenRow.model_validate(result.scalar_one())


async def get_slack_bot_token(
    session: AsyncSession,
    *,
    team_id: str,
) -> SlackBotTokenRow | None:
    orm = await session.get(SlackBotToken, team_id)
    if orm is None:
        return None
    return SlackBotTokenRow.model_validate(orm)


async def count_slack_bot_tokens(session: AsyncSession) -> int:
    """Return the total number of slack_bot_tokens rows. Read-only."""
    stmt = select(func.count()).select_from(SlackBotToken)
    return int((await session.execute(stmt)).scalar_one())


async def delete_slack_bot_token(
    session: AsyncSession,
    *,
    team_id: str,
    stored_at_or_before: datetime | None = None,
) -> int:
    """Delete the slack_bot_tokens row for a workspace. Idempotent.

    With `stored_at_or_before`, only a token stored (or re-stored) no later
    than that moment is deleted: a token a reinstall wrote afterwards stays.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.
    """
    stmt = delete(SlackBotToken).where(SlackBotToken.team_id == team_id)
    if stored_at_or_before is not None:
        stmt = stmt.where(SlackBotToken.updated_at <= stored_at_or_before)
    result = await session.execute(stmt)
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount

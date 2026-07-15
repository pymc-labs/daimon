"""Async store for slack_user_tokens (xoxp hybrid read model).

UPSERT on (team_id, slack_user_id): re-connecting replaces the row with fresh
ciphertext and scopes. Pure ciphertext-bytes in/out; never sees the Fernet key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import SlackUserToken
from daimon.core.stores.domain import SlackUserTokenRow
from sqlalchemy import CursorResult, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_slack_user_token(
    session: AsyncSession,
    *,
    team_id: str,
    slack_user_id: str,
    encrypted_token: bytes,
    scopes: str,
    expires_at: datetime | None = None,
    encrypted_refresh_token: bytes | None = None,
) -> SlackUserTokenRow:
    now = datetime.now(tz=UTC)
    stmt = pg_insert(SlackUserToken).values(
        team_id=team_id,
        slack_user_id=slack_user_id,
        encrypted_token=encrypted_token,
        scopes=scopes,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        encrypted_refresh_token=encrypted_refresh_token,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SlackUserToken.team_id, SlackUserToken.slack_user_id],
        set_={
            "encrypted_token": encrypted_token,
            "scopes": scopes,
            "updated_at": now,
            "expires_at": expires_at,
            "encrypted_refresh_token": encrypted_refresh_token,
        },
    ).returning(SlackUserToken)
    result = await session.execute(stmt)
    return SlackUserTokenRow.model_validate(result.scalar_one())


async def get_slack_user_token(
    session: AsyncSession,
    *,
    team_id: str,
    slack_user_id: str,
) -> SlackUserTokenRow | None:
    orm = await session.get(SlackUserToken, (team_id, slack_user_id))
    if orm is None:
        return None
    return SlackUserTokenRow.model_validate(orm)


async def delete_slack_user_token(
    session: AsyncSession,
    *,
    team_id: str,
    slack_user_id: str,
) -> int:
    """Delete the row for one (workspace, user). Idempotent; returns rowcount."""
    result = await session.execute(
        delete(SlackUserToken).where(
            SlackUserToken.team_id == team_id,
            SlackUserToken.slack_user_id == slack_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount

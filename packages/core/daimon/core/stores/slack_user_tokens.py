"""Async store for slack_user_tokens (xoxp hybrid read model).

UPSERT on (team_id, slack_user_id): re-connecting replaces the row with fresh
ciphertext and scopes. Pure ciphertext-bytes in/out; never sees the Fernet key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from daimon.core._models import PlatformPrincipal, SlackBotToken, SlackUserToken, Tenant
from daimon.core.stores.domain import SlackUserTokenRow
from sqlalchemy import CursorResult, delete, exists, select
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


async def delete_workspace_tokens(
    session: AsyncSession, *, tenant_id: uuid.UUID, team_id: str
) -> int:
    """Delete a Slack tenant's bot token and its own people's user tokens.

    Both tables are keyed by workspace id with no tenant column, so the id alone is not
    proof a row is this tenant's (Enterprise Grid orgs share people and ids across
    workspaces). A user token goes only when its user is one of this tenant's Slack
    principals, and the bot token only while this tenant is the one that owns `team_id`.
    """
    people = select(PlatformPrincipal.external_id).where(
        PlatformPrincipal.tenant_id == tenant_id, PlatformPrincipal.platform == "slack"
    )
    users = await session.execute(
        delete(SlackUserToken).where(
            SlackUserToken.team_id == team_id, SlackUserToken.slack_user_id.in_(people)
        )
    )
    owner = exists().where(
        Tenant.id == tenant_id, Tenant.platform == "slack", Tenant.external_id == team_id
    )
    bots = await session.execute(
        delete(SlackBotToken).where(SlackBotToken.team_id == team_id, owner)
    )
    return cast(CursorResult[Any], users).rowcount + cast(CursorResult[Any], bots).rowcount

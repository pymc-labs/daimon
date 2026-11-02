"""Store for web_node_tokens: a person's exchanged tokens, as ciphertext in and out."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import WebNodeToken
from daimon.core.stores.domain import WebNodeTokenRow
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_node_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    token_key: str,
    access_token_enc: bytes,
    expires_at: datetime,
) -> WebNodeTokenRow:
    stmt = (
        insert(WebNodeToken)
        .values(
            tenant_id=tenant_id,
            person_id=person_id,
            token_key=token_key,
            access_token_enc=access_token_enc,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=[WebNodeToken.person_id, WebNodeToken.token_key],
            set_={"access_token_enc": access_token_enc, "expires_at": expires_at},
        )
        .returning(WebNodeToken)
    )
    return WebNodeTokenRow.model_validate((await session.execute(stmt)).scalar_one())


async def get_node_token(
    session: AsyncSession, *, person_id: uuid.UUID, token_key: str
) -> WebNodeTokenRow | None:
    orm = await session.scalar(
        select(WebNodeToken).where(
            WebNodeToken.person_id == person_id, WebNodeToken.token_key == token_key
        )
    )
    return None if orm is None else WebNodeTokenRow.model_validate(orm)


async def delete_node_token(session: AsyncSession, *, person_id: uuid.UUID, token_key: str) -> None:
    """Forget a token the server stopped accepting, so the next call exchanges anew."""
    await session.execute(
        delete(WebNodeToken).where(
            WebNodeToken.person_id == person_id, WebNodeToken.token_key == token_key
        )
    )

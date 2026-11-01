"""Store for web_oidc_tokens: a person's provider tokens, as ciphertext in and out."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import WebOidcToken
from daimon.core.stores.domain import WebOidcTokenRow
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_oidc_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    issuer: str,
    access_token_enc: bytes,
    refresh_token_enc: bytes | None,
    expires_at: datetime,
) -> WebOidcTokenRow:
    new = insert(WebOidcToken).values(
        tenant_id=tenant_id,
        person_id=person_id,
        issuer=issuer,
        access_token_enc=access_token_enc,
        refresh_token_enc=refresh_token_enc,
        expires_at=expires_at,
    )
    stmt = new.on_conflict_do_update(
        index_elements=[WebOidcToken.person_id, WebOidcToken.issuer],
        set_={
            "access_token_enc": access_token_enc,
            # A sign-in that brings no refresh token keeps the one already stored.
            "refresh_token_enc": func.coalesce(
                new.excluded.refresh_token_enc, WebOidcToken.refresh_token_enc
            ),
            "expires_at": expires_at,
        },
    ).returning(WebOidcToken)
    return WebOidcTokenRow.model_validate((await session.execute(stmt)).scalar_one())


async def get_oidc_token(
    session: AsyncSession, *, person_id: uuid.UUID, issuer: str
) -> WebOidcTokenRow | None:
    orm = (
        await session.execute(
            select(WebOidcToken).where(
                WebOidcToken.person_id == person_id, WebOidcToken.issuer == issuer
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else WebOidcTokenRow.model_validate(orm)


async def lock_oidc_tokens(
    session: AsyncSession, *, person_ids: list[uuid.UUID], issuer: str
) -> list[WebOidcTokenRow]:
    """The rows of one sign-in's people, locked in a fixed order until the transaction ends.

    Refresh tokens rotate, so a refresh holds these locks: a second refresh, in any
    process, waits and then reads the rotated token.
    """
    rows = await session.execute(
        select(WebOidcToken)
        .where(WebOidcToken.person_id.in_(person_ids), WebOidcToken.issuer == issuer)
        .order_by(WebOidcToken.person_id)
        .with_for_update()
    )
    return [WebOidcTokenRow.model_validate(orm) for orm in rows.scalars()]

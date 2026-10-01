"""Short-lived, caller-bound control contexts for platform turns."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import TurnOrigin
from daimon.core.stores.domain import Role, TurnOriginRow
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_origin(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    configuration_target_ma_agent_id: str | None,
    configuration_target_name: str | None,
    role: Role,
    expires_at: datetime,
    now: datetime,
    is_setup: bool = False,
) -> TurnOriginRow:
    await session.execute(delete(TurnOrigin).where(TurnOrigin.expires_at <= now))
    origin = TurnOrigin(
        id=uuid.uuid4(),
        is_setup=is_setup,
        created_at=now,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        responder_ma_agent_id=responder_ma_agent_id,
        responder_name=responder_name,
        configuration_target_ma_agent_id=configuration_target_ma_agent_id,
        configuration_target_name=configuration_target_name,
        role=role.value,
        expires_at=expires_at,
    )
    session.add(origin)
    await session.flush()
    return TurnOriginRow.model_validate(origin)


async def get_active_origin(
    session: AsyncSession,
    *,
    origin_id: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    now: datetime,
    for_update: bool = False,
) -> TurnOriginRow | None:
    statement = select(TurnOrigin).where(
        TurnOrigin.id == origin_id,
        TurnOrigin.tenant_id == tenant_id,
        TurnOrigin.account_id == account_id,
        TurnOrigin.platform == platform,
        TurnOrigin.expires_at > now,
    )
    if for_update:
        statement = statement.with_for_update()
    origin = await session.scalar(statement)
    return TurnOriginRow.model_validate(origin) if origin is not None else None


async def update_origin_target(
    session: AsyncSession,
    *,
    origin_id: uuid.UUID,
    configuration_target_ma_agent_id: str,
    configuration_target_name: str,
) -> TurnOriginRow:
    origin = (
        await session.execute(
            update(TurnOrigin)
            .where(TurnOrigin.id == origin_id)
            .values(
                configuration_target_ma_agent_id=configuration_target_ma_agent_id,
                configuration_target_name=configuration_target_name,
            )
            .returning(TurnOrigin)
        )
    ).scalar_one()
    return TurnOriginRow.model_validate(origin)


async def delete_origin(session: AsyncSession, *, origin_id: uuid.UUID) -> None:
    await session.execute(delete(TurnOrigin).where(TurnOrigin.id == origin_id))

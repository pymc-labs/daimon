"""Previewed `add_skill` calls a person confirms in their next chat message.

Where approval cards are off, a chat preview records one row bound to the
previewing turn (`TurnOriginRow`): its person, thread and target agent, and
the previewed content's hash. Only that person's next turn in that thread may
consume it, once, before it expires. The model never confirms in the turn that
previewed, and never for another person.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal

from daimon.core._models import PendingSkillAdd, TurnOrigin
from daimon.core.stores.domain import TurnOriginRow
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

PENDING_SKILL_ADD_TTL: Final = timedelta(minutes=15)
"""How long a preview waits for its person's confirming message."""

ConfirmRefusal = Literal["no_preview", "same_turn", "not_next_message"]


async def record_pending_skill_add(
    session: AsyncSession,
    *,
    origin: TurnOriginRow,
    ma_agent_id: str,
    content_hash: str,
    now: datetime,
) -> None:
    await session.execute(delete(PendingSkillAdd).where(PendingSkillAdd.expires_at <= now))
    session.add(
        PendingSkillAdd(
            tenant_id=origin.tenant_id,
            account_id=origin.account_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            ma_agent_id=ma_agent_id,
            content_hash=content_hash,
            preview_origin_id=origin.id,
            created_at=now,
            expires_at=now + PENDING_SKILL_ADD_TTL,
        )
    )


async def consume_pending_skill_add(
    session: AsyncSession,
    *,
    origin: TurnOriginRow,
    ma_agent_id: str,
    content_hash: str,
    now: datetime,
) -> ConfirmRefusal | None:
    """Consume the preview ``origin``'s person confirms now; None when consumed.

    The confirming turn must be a later one than the preview's, and the first
    turn that person started in the thread since it.
    """
    pending = await session.scalar(
        select(PendingSkillAdd)
        .where(
            PendingSkillAdd.tenant_id == origin.tenant_id,
            PendingSkillAdd.account_id == origin.account_id,
            PendingSkillAdd.platform == origin.platform,
            PendingSkillAdd.thread_id == origin.thread_id,
            PendingSkillAdd.ma_agent_id == ma_agent_id,
            PendingSkillAdd.content_hash == content_hash,
            PendingSkillAdd.consumed_at.is_(None),
            PendingSkillAdd.expires_at > now,
        )
        .order_by(PendingSkillAdd.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if pending is None:
        return "no_preview"
    if pending.preview_origin_id == origin.id or origin.created_at <= pending.created_at:
        return "same_turn"
    turn_between = await session.scalar(
        select(
            exists().where(
                TurnOrigin.tenant_id == origin.tenant_id,
                TurnOrigin.account_id == origin.account_id,
                TurnOrigin.platform == origin.platform,
                TurnOrigin.thread_id == origin.thread_id,
                TurnOrigin.id != origin.id,
                TurnOrigin.created_at > pending.created_at,
                TurnOrigin.created_at < origin.created_at,
            )
        )
    )
    if turn_between:
        return "not_next_message"
    pending.consumed_at = now
    return None

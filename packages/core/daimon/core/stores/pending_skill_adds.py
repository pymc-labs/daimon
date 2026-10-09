"""Previewed `add_skill` calls a person confirms with an explicit yes as their next message.

Where approval cards are off, a chat preview records one row bound to the
previewing turn (`TurnOriginRow`): its person, thread and target agent, and
the previewed content's hash. When that person's next turn in the thread
starts, the adapter's own copy of their message settles it
(`resolve_pending_skill_adds`): an explicit yes approves it for that turn
alone, anything else cancels it. `add_skill` then consumes it once, from that
turn, before it expires. Nothing the model passes decides the confirmation.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final

from daimon.core._models import PendingSkillAdd
from daimon.core.stores.domain import TurnOriginRow
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

PENDING_SKILL_ADD_TTL: Final = timedelta(minutes=15)
"""How long a preview waits for its person's confirming message."""

AFFIRMATIVES: Final = frozenset({"yes", "y", "confirm", "approve"})
"""The whole replies that confirm a preview, once mentions and closing punctuation go."""

# Discord <@123>/<@!123>/<@&123>, Slack <@U123>/<@U123|name>, Teams <at>name</at>.
_MENTION = re.compile(r"<@[!&]?[\w|.-]+>|<at>.*?</at>", re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    """Whether a person's message is an explicit yes and nothing else."""
    reply = _MENTION.sub(" ", text).strip().rstrip(".!").strip().lower()
    return reply in AFFIRMATIVES


async def record_pending_skill_add(
    session: AsyncSession,
    *,
    origin: TurnOriginRow,
    ma_agent_id: str,
    content_hash: str,
    now: datetime,
) -> None:
    """Open a preview for ``origin``'s person, replacing their open one for this agent.

    One upsert on the open-preview unique index, so concurrent previews leave
    exactly one open row and the person's yes answers only the one that won.
    """
    await session.execute(delete(PendingSkillAdd).where(PendingSkillAdd.expires_at <= now))
    preview = {
        "content_hash": content_hash,
        "preview_origin_id": origin.id,
        "created_at": now,
        "expires_at": now + PENDING_SKILL_ADD_TTL,
        "approved_origin_id": None,
    }
    await session.execute(
        insert(PendingSkillAdd)
        .values(
            tenant_id=origin.tenant_id,
            account_id=origin.account_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            ma_agent_id=ma_agent_id,
            **preview,
        )
        .on_conflict_do_update(
            index_elements=[
                PendingSkillAdd.tenant_id,
                PendingSkillAdd.account_id,
                PendingSkillAdd.platform,
                PendingSkillAdd.thread_id,
                PendingSkillAdd.ma_agent_id,
            ],
            index_where=PendingSkillAdd.consumed_at.is_(None),
            set_=preview,
        )
    )


async def resolve_pending_skill_adds(
    session: AsyncSession, *, origin: TurnOriginRow, message_text: str, now: datetime
) -> None:
    """Settle this person's open previews in this thread with their new message.

    An explicit yes (`is_affirmative`) approves them for ``origin`` alone;
    anything else cancels them. Either way their next message has been used.
    """
    still_open = (
        PendingSkillAdd.tenant_id == origin.tenant_id,
        PendingSkillAdd.account_id == origin.account_id,
        PendingSkillAdd.platform == origin.platform,
        PendingSkillAdd.thread_id == origin.thread_id,
        PendingSkillAdd.consumed_at.is_(None),
        PendingSkillAdd.approved_origin_id.is_(None),
        PendingSkillAdd.expires_at > now,
    )
    settled = (
        {"approved_origin_id": origin.id} if is_affirmative(message_text) else {"consumed_at": now}
    )
    await session.execute(update(PendingSkillAdd).where(*still_open).values(**settled))


async def consume_pending_skill_add(
    session: AsyncSession,
    *,
    origin: TurnOriginRow,
    ma_agent_id: str,
    content_hash: str,
    now: datetime,
) -> bool:
    """Consume the preview ``origin``'s own message approved; False when there is none.

    One conditional update, so two calls in that turn can't both consume it.
    """
    consumed = await session.scalar(
        update(PendingSkillAdd)
        .where(
            PendingSkillAdd.tenant_id == origin.tenant_id,
            PendingSkillAdd.account_id == origin.account_id,
            PendingSkillAdd.platform == origin.platform,
            PendingSkillAdd.thread_id == origin.thread_id,
            PendingSkillAdd.ma_agent_id == ma_agent_id,
            PendingSkillAdd.content_hash == content_hash,
            PendingSkillAdd.approved_origin_id == origin.id,
            PendingSkillAdd.consumed_at.is_(None),
            PendingSkillAdd.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(PendingSkillAdd.id)
    )
    return consumed is not None

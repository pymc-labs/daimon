"""Async store for message_feedback — thumbs-up/down reaction votes.

No try/except anywhere in this module — exceptions propagate to the adapter
boundary. Callers own the transaction; every write ends with `await
session.flush()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from daimon.core._models import MessageFeedback
from daimon.core.message_feedback import Vote
from daimon.core.stores.domain import MessageFeedbackRow, RecordVoteResult
from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def record_vote(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    message_id: str,
    channel_id: str,
    platform_user_id: str,
    account_id: uuid.UUID | None,
    ma_session_id: str | None,
    vote: Vote,
) -> RecordVoteResult:
    """Upsert the voter's vote for `message_id`, reporting their prior vote.

    Two statements. First, read (and row-lock) the current vote for the
    conflict target — this is `previous_vote`. Second, upsert on the
    tenant/message/voter uniqueness key, updating `vote`, `channel_id`,
    `account_id`, and `ma_session_id`.

    `feedback_text` is deliberately absent from the update: a later re-vote
    must not erase text the person already wrote for a prior vote on the same
    message.

    One race the row lock does not cover: two concurrent FIRST votes by the
    same person on the same message both read no prior row, so both report
    `previous_vote=None` and a caller-side private follow-up could fire
    twice. This is bounded, self-inflicted (one person, one message), and
    cheaper than a table-level lock.
    """
    previous_vote = (
        await session.execute(
            select(MessageFeedback.vote)
            .where(
                MessageFeedback.tenant_id == tenant_id,
                MessageFeedback.message_id == message_id,
                MessageFeedback.platform_user_id == platform_user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    stmt = (
        pg_insert(MessageFeedback)
        .values(
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            message_id=message_id,
            channel_id=channel_id,
            platform_user_id=platform_user_id,
            ma_session_id=ma_session_id,
            vote=vote,
        )
        .on_conflict_do_update(
            constraint="uq_message_feedback_tenant_message_user",
            set_={
                "vote": vote,
                "channel_id": channel_id,
                "account_id": account_id,
                "ma_session_id": ma_session_id,
                "updated_at": func.now(),
            },
        )
        .returning(MessageFeedback)
    )
    orm = (await session.execute(stmt)).scalar_one()
    await session.flush()
    return RecordVoteResult(
        row=MessageFeedbackRow.model_validate(orm),
        previous_vote=previous_vote,
    )


async def attach_feedback_text(
    session: AsyncSession,
    *,
    feedback_id: uuid.UUID,
    platform_user_id: str,
    feedback_text: str,
) -> MessageFeedbackRow | None:
    """Attach free text to an existing vote row, gated on the voter's own id.

    The `platform_user_id` predicate is a real authorization gate, not a
    convenience filter: it means a click carrying someone else's row id
    writes nothing. `None` means "no such row for this person" and
    deliberately does not distinguish a purged row from a mismatched one —
    the caller must not try to. Deliberately does NOT filter on the row's
    current vote value, so text written after a later flip back to a
    positive vote still lands.
    """
    stmt = (
        update(MessageFeedback)
        .where(
            MessageFeedback.id == feedback_id,
            MessageFeedback.platform_user_id == platform_user_id,
        )
        .values(feedback_text=feedback_text, updated_at=func.now())
        .returning(MessageFeedback)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return MessageFeedbackRow.model_validate(orm)


async def attach_feedback_details(
    session: AsyncSession,
    *,
    feedback_id: uuid.UUID,
    platform_user_id: str,
    feedback_text: str | None,
    feedback_reasons: Sequence[str],
) -> MessageFeedbackRow | None:
    """Store a whole "What went wrong?" form on the voter's own row.

    Like `attach_feedback_text`, gated on `platform_user_id`, and `None` means
    no such row for this person. The form is the person's complete answer, so
    both fields are replaced: a later submission without text clears earlier
    text, and no reasons picked stores NULL.
    """
    stmt = (
        update(MessageFeedback)
        .where(
            MessageFeedback.id == feedback_id,
            MessageFeedback.platform_user_id == platform_user_id,
        )
        .values(
            feedback_text=feedback_text,
            feedback_reasons=list(feedback_reasons) or None,
            updated_at=func.now(),
        )
        .returning(MessageFeedback)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if orm is None:
        return None
    return MessageFeedbackRow.model_validate(orm)


async def delete_message_feedback_for_platform_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> int:
    """Delete one platform user's vote rows in one tenant. Idempotent.

    The principal-scoped counterpart to
    `delete_message_feedback_for_account`: keyed by the identity a vote
    actually carries, so it reaches rows written before the voter had an
    `accounts` row. Deliberately does NOT OR in an account-id predicate —
    purging ONE principal must not erase the same account's votes cast under
    a different tenant or a different platform identity.

    Tenant-scoped for the same reason `credential_requests` and
    `wizard_session` are: `platform_user_id` is not globally unique across
    platforms (Slack user ids are workspace-scoped).
    """
    result = await session.execute(
        delete(MessageFeedback).where(
            MessageFeedback.tenant_id == tenant_id,
            MessageFeedback.platform_user_id == platform_user_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


def _account_or_platform_user_predicate(
    *, account_id: uuid.UUID, platform_user_keys: Sequence[tuple[uuid.UUID, str]]
) -> ColumnElement[bool]:
    """Shared predicate for the delete/count pair — they MUST stay identical.

    Starts from `account_id == account_id`; when `platform_user_keys` is
    non-empty, ORs in a `(tenant_id, platform_user_id)` membership check. A
    row written before the voter had an `accounts` row carries `account_id
    = NULL` and is unreachable by an account-keyed delete alone, which
    would be a silent erasure gap — the second predicate closes it.
    """
    predicates: list[ColumnElement[bool]] = [MessageFeedback.account_id == account_id]
    if platform_user_keys:
        predicates.append(
            tuple_(MessageFeedback.tenant_id, MessageFeedback.platform_user_id).in_(
                list(platform_user_keys)
            )
        )
    return or_(*predicates)


async def delete_message_feedback_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Delete every vote row attributable to `account_id`. Idempotent.

    Must build the SAME predicate as `count_message_feedback_for_account`, or
    the /privacy cascade preview diverges from what this actually deletes.
    """
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    result = await session.execute(delete(MessageFeedback).where(predicate))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_message_feedback_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Sequence[tuple[uuid.UUID, str]],
) -> int:
    """Count vote rows that `delete_message_feedback_for_account` would delete.

    Read-only. Must build the SAME predicate as the delete, or the /privacy
    cascade preview diverges from what the delete actually removes.
    """
    predicate = _account_or_platform_user_predicate(
        account_id=account_id, platform_user_keys=platform_user_keys
    )
    stmt = select(func.count()).select_from(MessageFeedback).where(predicate)
    return int((await session.execute(stmt)).scalar_one())

"""Operational rows that name an account by id without a foreign key.

Account erasure counts and deletes them here, across every tenant, since the
account id alone identifies them.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, cast

from daimon.core._models import (
    BackendSwitchCard,
    CredentialRequest,
    LongJobCancelAuditRecord,
    TaskContinuation,
    ThreadSession,
    TurnOutcome,
)
from sqlalchemy import ColumnElement, and_, delete, func, not_, or_, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AccountRecords:
    task_continuations: int = 0
    turn_outcomes: int = 0
    thread_sessions: int = 0
    credential_requests: int = 0
    switch_cards: int = 0
    long_job_cancel_audit: int = 0


def _conditions(
    account_id: uuid.UUID, platform_user_keys: Collection[tuple[uuid.UUID, str]]
) -> list[tuple[Any, ColumnElement[bool]]]:
    requests = CredentialRequest.account_id == account_id
    if platform_user_keys:
        # Rows a principal-scoped delete already reaches are counted there.
        keys = tuple_(CredentialRequest.tenant_id, CredentialRequest.requester_platform_user_id)
        requests = and_(requests, not_(keys.in_(list(platform_user_keys))))
    return [
        (TaskContinuation, TaskContinuation.requester_account_id == account_id),
        (TurnOutcome, TurnOutcome.account_id == account_id),
        (ThreadSession, ThreadSession.account_id == account_id),
        (CredentialRequest, requests),
        (BackendSwitchCard, BackendSwitchCard.account_id == account_id),
        (
            LongJobCancelAuditRecord,
            or_(
                LongJobCancelAuditRecord.owner_account_id == account_id,
                LongJobCancelAuditRecord.requested_by == account_id,
            ),
        ),
    ]


async def count_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Collection[tuple[uuid.UUID, str]] = (),
) -> AccountRecords:
    counts = [
        await session.scalar(select(func.count()).select_from(model).where(condition)) or 0
        for model, condition in _conditions(account_id, platform_user_keys)
    ]
    return AccountRecords(*counts)


async def delete_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    platform_user_keys: Collection[tuple[uuid.UUID, str]] = (),
) -> AccountRecords:
    counts = [
        cast(CursorResult[Any], await session.execute(delete(model).where(condition))).rowcount
        for model, condition in _conditions(account_id, platform_user_keys)
    ]
    return AccountRecords(*counts)

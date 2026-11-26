"""Operational rows that name an account by id without a foreign key.

Account erasure counts and deletes them here, across every tenant, since the
account id alone identifies them.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, Literal, cast

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

AccountTable = Literal[
    "task_continuations",
    "turn_outcomes",
    "thread_sessions",
    "credential_requests",
    "switch_cards",
    "long_job_cancel_audit",
]


def _condition(
    table: AccountTable, account_id: uuid.UUID, requester_keys: Collection[tuple[uuid.UUID, str]]
) -> tuple[Any, ColumnElement[bool]]:
    match table:
        case "task_continuations":
            return TaskContinuation, TaskContinuation.requester_account_id == account_id
        case "turn_outcomes":
            return TurnOutcome, TurnOutcome.account_id == account_id
        case "thread_sessions":
            return ThreadSession, ThreadSession.account_id == account_id
        case "switch_cards":
            return BackendSwitchCard, BackendSwitchCard.account_id == account_id
        case "long_job_cancel_audit":
            return LongJobCancelAuditRecord, or_(
                LongJobCancelAuditRecord.owner_account_id == account_id,
                LongJobCancelAuditRecord.requested_by == account_id,
            )
        case "credential_requests":
            requests = CredentialRequest.account_id == account_id
            if requester_keys:
                # Rows a principal-scoped delete already reaches are counted there.
                keys = tuple_(
                    CredentialRequest.tenant_id, CredentialRequest.requester_platform_user_id
                )
                requests = and_(requests, not_(keys.in_(list(requester_keys))))
            return CredentialRequest, requests


async def count_for_account(
    session: AsyncSession,
    *,
    table: AccountTable,
    account_id: uuid.UUID,
    requester_keys: Collection[tuple[uuid.UUID, str]] = (),
) -> int:
    """Rows of `table` naming the account, less those `requester_keys` already reach."""
    model, condition = _condition(table, account_id, requester_keys)
    return await session.scalar(select(func.count()).select_from(model).where(condition)) or 0


async def delete_for_account(
    session: AsyncSession, *, table: AccountTable, account_id: uuid.UUID
) -> int:
    model, condition = _condition(table, account_id, ())
    return cast(CursorResult[Any], await session.execute(delete(model).where(condition))).rowcount

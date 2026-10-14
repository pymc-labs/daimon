"""Explicitly enabled credential-card retirement with retryable in-place edits."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from daimon.core.stores.credential_requests import (
    acknowledge_credential_card_expiry,
    list_pending_credential_card_expiries,
    retire_expired_credential_requests,
)
from daimon.core.stores.domain import CredentialRequestRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EditExpiredCredentialCard = Callable[[CredentialRequestRow], Awaitable[bool]]
_log = structlog.get_logger(__name__)
_EDIT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CredentialFormExpiryPoll:
    """Component counts plus a retry cursor, never a catalog verdict."""

    retired: int
    attempted: int
    acknowledged: int
    next_after_token: str | None = field(repr=False)


async def poll_credential_form_expiries_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    edit: EditExpiredCredentialCard,
    tenant_id: uuid.UUID | None = None,
    limit: int = 20,
    after_token: str | None = None,
) -> CredentialFormExpiryPoll:
    """Commit expiry CAS, edit without a transaction, then acknowledge the same card.

    Retain next_after_token between calls and pass it as after_token. An exhausted
    page resets it to None, allowing failures to retry without blocking later rows.
    None/truthy placeholders from an old fire-and-forget editor are not acknowledgements.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("credential expiry requires an aware time")
    if not 1 <= limit <= 100:
        raise ValueError("credential expiry limit must be between one and 100")
    async with sessionmaker.begin() as session:
        retired = await retire_expired_credential_requests(
            session, now=now, tenant_id=tenant_id, limit=limit
        )
    async with sessionmaker() as session:
        rows = await list_pending_credential_card_expiries(
            session, now=now, tenant_id=tenant_id, limit=limit, after_token=after_token
        )
    acknowledged = 0
    for row in rows:
        try:
            edited = await asyncio.wait_for(edit(row), timeout=_EDIT_TIMEOUT_S)
        except Exception as error:
            # Per-card adapter boundary: a failed edit must not strand other
            # cards in this batch. Cancellation still propagates; pending is durable.
            _log.warning("credential_expiry.edit_failed", err_type=type(error).__name__)
            continue
        if edited is True:
            async with sessionmaker.begin() as session:
                acknowledged += int(await acknowledge_credential_card_expiry(session, row=row))
    return CredentialFormExpiryPoll(
        retired=retired,
        attempted=len(rows),
        acknowledged=acknowledged,
        next_after_token=rows[-1].token if len(rows) == limit else None,
    )


async def run_credential_form_expiry_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    edit: EditExpiredCredentialCard,
    should_stop: Callable[[], bool],
    interval_s: float = 15.0,
) -> None:
    """Single platform posting owner's loop; nothing calls this unless explicitly enabled."""
    after_token: str | None = None
    while not should_stop():
        try:
            result = await poll_credential_form_expiries_once(
                sessionmaker, now=datetime.now(UTC), edit=edit, after_token=after_token
            )
            after_token = result.next_after_token
        except Exception as error:
            _log.warning("credential_expiry.poll_failed", err_type=type(error).__name__)
        await asyncio.sleep(interval_s)

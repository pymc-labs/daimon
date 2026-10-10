"""Failed external saves release only their own attempt and preserve waiting work."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.credential_requests import mint_request_token
from daimon.core.credential_submit import guard_credential_save, settle_credential_submit
from daimon.core.posted_controls import card_for_request
from daimon.core.stores import credential_requests as store
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _request(db: async_sessionmaker[AsyncSession]) -> CredentialRequestRow:
    async with db.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        return await store.create_credential_request(
            session,
            token=mint_request_token(),
            kind="mcp",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            target="service",
            mcp_server_url="https://example.com/mcp",
            requester_platform_user_id="requester",
            channel_id="parent",
            parent_channel_id="parent",
            origin_thread_id="thread",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="agent_test",
            target_name="tester",
            requested_work="finish the report after connecting",
        )


async def _consume(
    db: async_sessionmaker[AsyncSession], row: CredentialRequestRow
) -> CredentialRequestRow:
    async with db.begin() as session:
        consumed = await store.consume_credential_request(
            session, token=row.token, now=datetime.now(UTC)
        )
    assert consumed is not None
    return consumed


@pytest.mark.parametrize(
    "failure", ["rejected", "handled_storage_error", "exception", "timeout", "cancelled"]
)
async def test_failed_save_finalizes_and_successful_retry_resumes_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    failure: str,
) -> None:
    db = db_session_factory
    row = await _request(db)
    consumed = await _consume(db, row)
    edit = AsyncMock()
    stopped = asyncio.Event()

    async def save() -> None:
        async with guard_credential_save(db, row=consumed, edit_retry=edit, timeout_seconds=0.02):
            if failure == "rejected":
                async with db.begin() as session:
                    await store.set_credential_request_outcome(
                        session, token=row.token, outcome="token_rejected"
                    )
            elif failure == "handled_storage_error":
                await settle_credential_submit(
                    db, row=consumed, platform="discord", outcome="write_failed", carries_work=False
                )
            elif failure == "exception":
                raise RuntimeError("private-submitted-value")
            else:
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

    if failure == "cancelled":
        task = asyncio.create_task(save())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await save()
    async with db() as session:
        retry = await store.peek_credential_request(session, token=row.token)
        assert await get_continuation(session, idempotency_key=row.idempotency_key) is None
    assert retry is not None and retry.used_at is None
    assert retry.outcome == ("token_rejected" if failure == "rejected" else "write_failed")
    edit.assert_awaited_once()
    reason = edit.await_args.args[1]
    assert "Try again" in reason and "private-submitted-value" not in reason
    if failure in ("timeout", "cancelled"):
        assert stopped.is_set(), "stop the old write before offering a retry"
    if failure == "timeout":
        assert "too long" in reason
    card = card_for_request(retry, state="requested", retry_reason=reason)
    assert card.buttons and "Saving…" not in card.headline

    second = await _consume(db, retry)
    assert second.outcome is None, "the new attempt clears the previous failure"
    async with guard_credential_save(db, row=second, edit_retry=edit):
        assert await settle_credential_submit(db, row=second, platform="discord", outcome="applied")
    async with db() as session:
        saved = await store.peek_credential_request(session, token=row.token)
        continuation = await get_continuation(session, idempotency_key=row.idempotency_key)
        assert (
            await store.consume_credential_request(session, token=row.token, now=datetime.now(UTC))
            is None
        )
    assert saved is not None and saved.used_at is not None and saved.outcome == "applied"
    assert continuation is not None and continuation.requested_work == row.requested_work
    assert edit.await_count == 1, "successful completion keeps its normal receipt"


async def test_old_failure_cannot_release_a_later_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db = db_session_factory
    first = await _consume(db, await _request(db))
    async with db.begin() as session:
        retry = await store.release_failed_credential_request(
            session, row=first, outcome="write_failed"
        )
    assert retry is not None
    second = await _consume(db, retry)
    async with db.begin() as session:
        assert (
            await store.release_failed_credential_request(
                session, row=first, outcome="write_failed"
            )
            is None
        )
    async with db() as session:
        held = await store.peek_credential_request(session, token=first.token)
    assert held is not None and held.used_at == second.used_at


async def test_expiry_during_save_still_leaves_a_usable_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core._models import CredentialRequest
    from sqlalchemy import update

    db = db_session_factory
    consumed = await _consume(db, await _request(db))
    async with db.begin() as session:
        await session.execute(
            update(CredentialRequest)
            .where(CredentialRequest.token == consumed.token)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    edit = AsyncMock()
    async with guard_credential_save(db, row=consumed, edit_retry=edit):
        raise RuntimeError("save failed after the form expired")
    retry = edit.await_args.args[0]
    assert retry.expires_at > datetime.now(UTC) + timedelta(minutes=29)
    assert (await _consume(db, retry)).token == consumed.token

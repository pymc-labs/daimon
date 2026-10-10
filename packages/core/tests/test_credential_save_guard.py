"""Failed external saves release only their own attempt and preserve waiting work."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.credential_requests import mint_request_token
from daimon.core.credential_submit import (
    STILL_SAVING_NOTICE,
    credential_card_edit,
    guard_credential_save,
    settle_credential_submit,
)
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


@pytest.mark.parametrize("failure", ["rejected", "handled_storage_error", "exception", "cancelled"])
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
        async with guard_credential_save(
            db, row=consumed, edit_retry=edit, edit_pending=AsyncMock()
        ):
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
    if failure == "cancelled":
        assert stopped.is_set(), "stop the old write before offering a retry"
    card = card_for_request(retry, state="requested", retry_reason=reason)
    assert card.buttons and "Saving…" not in card.headline

    second = await _consume(db, retry)
    assert second.outcome is None, "the new attempt clears the previous failure"
    async with guard_credential_save(db, row=second, edit_retry=edit, edit_pending=AsyncMock()):
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
    async with guard_credential_save(db, row=consumed, edit_retry=edit, edit_pending=AsyncMock()):
        raise RuntimeError("save failed after the form expired")
    retry = edit.await_args.args[0]
    assert retry.expires_at > datetime.now(UTC) + timedelta(minutes=29)
    assert (await _consume(db, retry)).token == consumed.token


@pytest.mark.parametrize("completion", ["success", "failure"])
async def test_deadline_updates_card_without_cancelling_or_releasing_save(
    db_session_factory: async_sessionmaker[AsyncSession], completion: str
) -> None:
    db = db_session_factory
    consumed = await _consume(db, await _request(db))
    notice_seen, finish = asyncio.Event(), asyncio.Event()
    receipts: list[str] = []
    retry = AsyncMock()

    async def pending(row: CredentialRequestRow, text: str) -> None:
        async with credential_card_edit(row, "received"):
            card = card_for_request(row, state="received", saving_notice=text)
            assert not card.buttons and card.footer == STILL_SAVING_NOTICE
            receipts.append(text)
            notice_seen.set()

    async def save() -> None:
        async with guard_credential_save(
            db, row=consumed, edit_retry=retry, edit_pending=pending, deadline_seconds=0.01
        ):
            await finish.wait()
            if completion == "failure":
                raise RuntimeError("private-submitted-value")
            await settle_credential_submit(db, row=consumed, platform="discord", outcome="applied")
            async with credential_card_edit(consumed, "applied"):
                receipts.append("success")

    task = asyncio.create_task(save())
    try:
        async with asyncio.timeout(5):
            await notice_seen.wait()
        assert not task.done(), "the deadline must leave the save running"
        async with db() as session:
            held = await store.peek_credential_request(session, token=consumed.token)
            assert held is not None and held.used_at == consumed.used_at and held.outcome is None
            assert await get_continuation(session, idempotency_key=held.idempotency_key) is None
        retry.assert_not_awaited()
    finally:
        finish.set()
        await task
    if completion == "success":
        assert receipts == [STILL_SAVING_NOTICE, "success"]
        retry.assert_not_awaited()
        async with db() as session:
            saved = await store.peek_credential_request(session, token=consumed.token)
            continuation = await get_continuation(session, idempotency_key=consumed.idempotency_key)
        assert saved is not None and saved.outcome == "applied" and saved.used_at is not None
        assert continuation is not None
    else:
        retry.assert_awaited_once()
        failed = retry.await_args.args[0]
        assert failed.used_at is None and failed.outcome == "write_failed"
        assert "private-submitted-value" not in retry.await_args.args[1]


async def test_final_receipt_waits_for_inflight_notice_and_cannot_be_overwritten(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    db = db_session_factory
    consumed = await _consume(db, await _request(db))
    editing, deliver = asyncio.Event(), asyncio.Event()
    receipts: list[str] = []

    async def pending(row: CredentialRequestRow, text: str) -> None:
        async with credential_card_edit(row, "received"):
            editing.set()
            await deliver.wait()
            receipts.append(text)

    async with guard_credential_save(
        db, row=consumed, edit_retry=AsyncMock(), edit_pending=pending, deadline_seconds=0
    ):
        async with asyncio.timeout(5):
            await editing.wait()
        await settle_credential_submit(db, row=consumed, platform="discord", outcome="applied")

        async def final() -> None:
            async with credential_card_edit(consumed, "applied"):
                receipts.append("success")

        final_task = asyncio.create_task(final())
        await asyncio.sleep(0)
        assert not final_task.done()
        deliver.set()
        await final_task
    assert receipts == [STILL_SAVING_NOTICE, "success"]


async def test_finalization_db_failure_does_not_leave_the_saving_placeholder(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    consumed = await _consume(db_session_factory, await _request(db_session_factory))
    retry, receipt = AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        store, "peek_credential_request", AsyncMock(side_effect=RuntimeError("secret"))
    )
    async with guard_credential_save(
        db_session_factory, row=consumed, edit_retry=retry, edit_pending=receipt
    ):
        raise RuntimeError("failed write")
    retry.assert_not_awaited()
    receipt.assert_awaited_once()
    assert receipt.await_args.args[1] == "Saving could not be confirmed. Ask Daimon for a new form."

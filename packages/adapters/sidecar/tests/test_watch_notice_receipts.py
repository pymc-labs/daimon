"""Owner notices leave a receipt per attempt and go out once across restarts and executors."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.continuity.watch_imports import ImportedWatch
from daimon.core.continuity.watch_notices import poll_watch_notices
from daimon.core.stores import watches
from daimon.core.stores.domain import WatchRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_watch_imports import definition as definition

pytestmark = pytest.mark.fresh_schema


async def _expired(
    sessions: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> WatchRow:
    now = datetime.now(UTC)
    async with sessions.begin() as db:
        row = await watches.import_watch(db, definition, now=now)
        await db.execute(
            text(
                "UPDATE watches SET status='expired', expires_at=:at, source_state='{}' "
                "WHERE id=:id"
            ),
            {"at": now - timedelta(minutes=5), "id": row.id},
        )
    return row


async def _receipts(sessions: async_sessionmaker[AsyncSession], row: WatchRow):
    async with sessions() as db:
        return await watches.list_notice_receipts(db, watch_id=row.id)


async def _expire_lease(sessions: async_sessionmaker[AsyncSession], row: WatchRow) -> None:
    async with sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE watches SET source_state=source_state || "
                "'{\"notice_lease_until\": null}'::jsonb WHERE id=:id"
            ),
            {"id": row.id},
        )


async def _crash_after_intent(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[WatchRow, watches.NoticeAttempt]:
    """A claim whose intent committed and whose process died before settling."""
    async with sessions.begin() as db:
        now = datetime.now(UTC)
        notice = await watches.claim_import_notice(db, now=now)
        assert notice is not None
        attempt = await watches.open_notice_attempt(db, notice[0], now=now)
        assert attempt is not None
    return notice[0], attempt


async def test_expiry_notice_is_sent_once_with_a_receipt_and_no_text(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    deliver = AsyncMock(return_value=watches.SentNotice("900", "901"))
    for _ in range(3):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    assert deliver.await_args is not None
    attempt = deliver.await_args.kwargs["attempt"]
    assert attempt.kind == "expired" and attempt.since is None and len(attempt.nonce) <= 25
    [receipt] = await _receipts(db_session_factory, row)
    assert (receipt.kind, receipt.generation, receipt.attempt) == ("expired", 1, 1)
    assert (receipt.outcome, receipt.channel_id, receipt.message_id) == ("sent", "900", "901")
    assert receipt.owner_account_id == row.owner_account_id
    assert receipt.settled_at is not None
    assert row.instructions not in repr(asdict(receipt))


async def test_failed_send_is_retried_under_the_same_notice(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    await poll_watch_notices(db_session_factory, deliver=AsyncMock(return_value=False))
    await _expire_lease(db_session_factory, row)
    deliver = AsyncMock(return_value=True)
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    assert deliver.await_args is not None
    # Even a failed attempt may have landed (a refused send cannot be told apart from
    # every unclear one), so the retry always reads the DM first.
    assert deliver.await_args.kwargs["attempt"].since is not None
    receipts = await _receipts(db_session_factory, row)
    assert [(r.generation, r.attempt, r.outcome) for r in receipts] == [
        (1, 1, "failed"),
        (1, 2, "sent"),
    ]


async def test_a_held_claim_is_not_sent_by_a_second_executor(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    await _crash_after_intent(db_session_factory)
    deliver = AsyncMock(return_value=True)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_not_awaited()
    [receipt] = await _receipts(db_session_factory, row)
    assert receipt.outcome == "pending"


async def test_restart_after_send_finds_the_message_instead_of_resending(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    _, crashed = await _crash_after_intent(db_session_factory)
    await _expire_lease(db_session_factory, row)
    # Delivery is told when the unresolved attempt began, and reports the message it found.
    found = AsyncMock(return_value=watches.SentNotice("900", "901"))
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=found)
    found.assert_awaited_once()
    assert found.await_args is not None
    resumed = found.await_args.kwargs["attempt"]
    assert resumed.since is not None and resumed.nonce == crashed.nonce
    assert resumed.generation == crashed.generation
    receipts = await _receipts(db_session_factory, row)
    assert [(r.attempt, r.outcome, r.message_id) for r in receipts] == [
        (1, "interrupted", None),
        (2, "sent", "901"),
    ]


async def test_a_sent_receipt_settles_a_resumed_claim_without_sending(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    stale, slow = await _crash_after_intent(db_session_factory)
    await _expire_lease(db_session_factory, row)

    async def stale_executor_finishes(*_args: object, **_kwargs: object) -> bool:
        # The first executor's send lands and records while the second is resuming.
        async with db_session_factory.begin() as db:
            await watches.close_notice_attempt(
                db, stale, slow, sent=watches.SentNotice("900", "901"), now=datetime.now(UTC)
            )
        return False

    await poll_watch_notices(db_session_factory, deliver=stale_executor_finishes)
    await _expire_lease(db_session_factory, row)
    deliver = AsyncMock(return_value=True)
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_not_awaited()
    receipts = await _receipts(db_session_factory, row)
    assert [(r.attempt, r.outcome) for r in receipts] == [(1, "sent"), (2, "failed")]
    async with db_session_factory() as db:
        current = await watches.get_watch(db, row.id)
    assert current is not None
    assert current.source_state["notice_pending"] is False
    assert current.source_state["notice_lease"] is None


async def test_receipts_outlive_the_watch_and_are_pruned(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    await poll_watch_notices(db_session_factory, deliver=AsyncMock(return_value=True))
    async with db_session_factory.begin() as db:
        await watches.delete_watch(db, row.id, now=datetime.now(UTC))
        assert await watches.get_watch(db, row.id) is None
    assert [r.outcome for r in await _receipts(db_session_factory, row)] == ["sent"]
    async with db_session_factory.begin() as db:
        later = datetime.now(UTC) + watches.NOTICE_RECEIPT_RETENTION + timedelta(minutes=1)
        await watches.prune_notice_receipts(db, now=later)
    assert await _receipts(db_session_factory, row) == []


async def test_a_send_that_times_out_after_discord_stored_it_is_not_sent_twice(
    db_session_factory: async_sessionmaker[AsyncSession], definition: ImportedWatch
) -> None:
    row = await _expired(db_session_factory, definition)
    owner_dm: list[str] = []

    async def stored_then_timed_out(
        _watch: WatchRow, _workspace: str, message: str, *, attempt: watches.NoticeAttempt
    ) -> watches.SentNotice | watches.Unclear:
        if attempt.since is not None and message in owner_dm:
            # The DM check finds the first message instead of sending again.
            return watches.SentNotice("900", "901")
        owner_dm.append(message)
        raise TimeoutError

    await poll_watch_notices(db_session_factory, deliver=stored_then_timed_out)
    [first] = await _receipts(db_session_factory, row)
    assert first.outcome == "interrupted"
    # Past Discord's nonce window: only the DM check stands between the owner and a repeat.
    await _expire_lease(db_session_factory, row)
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=stored_then_timed_out)
    assert len(owner_dm) == 1
    receipts = await _receipts(db_session_factory, row)
    assert [(r.attempt, r.outcome, r.message_id) for r in receipts] == [
        (1, "interrupted", None),
        (2, "sent", "901"),
    ]

"""Real Postgres expiry CAS, committed-before-edit and restart/ack proofs."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.credential_form_expiry import (
    poll_credential_form_expiries_once,
    run_credential_form_expiry_poller,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.stores import credential_requests as store
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


@pytest.fixture
def expiry_sm(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def seed(
    sm: async_sessionmaker[AsyncSession], *, expires_at: datetime = _NOW, **overrides: object
) -> CredentialRequestRow:
    async with sm.begin() as session:
        tenant = await make_tenant(session, platform="discord")
        account = await make_account(session, tenant=tenant)
        fields = dict(
            token=mint_request_token(),
            kind="env",
            tenant_id=tenant.id,
            account_id=account.id,
            agent_id=uuid.uuid4(),
            target="ACME_API_KEY",
            mcp_server_url=None,
            requester_platform_user_id="103",
            channel_id="101",
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="Daimon",
            requested_work="read the project",
            platform="discord",
            parent_channel_id="101",
            origin_thread_id="102",
            posted_message_id="104",
            expires_at=expires_at,
        )
        fields.update(overrides)
        return await store.create_credential_request(session, **fields)  # type: ignore[arg-type]


async def read(sm: async_sessionmaker[AsyncSession], token: str) -> CredentialRequestRow:
    async with sm() as session:
        row = await store.peek_credential_request(session, token=token)
    assert row is not None
    return row


async def test_expiry_boundary_is_authoritative_and_committed_before_edit(expiry_sm) -> None:
    row = await seed(expiry_sm)
    seen = []

    async def edit(retired: CredentialRequestRow) -> bool:
        assert expiry_sm.kw["bind"].sync_engine.pool.checkedout() == 0, (
            "no expiry transaction/connection may span Discord HTTP"
        )
        persisted = await read(expiry_sm, retired.token)
        assert persisted.used_at == _NOW
        assert persisted.outcome == "expired_card_pending"
        assert persisted.requested_work == "read the project"
        seen.append(retired)
        return True

    early = await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW - timedelta(microseconds=1), edit=edit
    )
    assert (early.retired, early.attempted, early.acknowledged) == (0, 0, 0)
    assert (await read(expiry_sm, row.token)).used_at is None
    due = await poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=edit)
    assert (due.retired, due.attempted, due.acknowledged) == (1, 1, 1)
    assert [item.token for item in seen] == [row.token]
    after = await read(expiry_sm, row.token)
    assert after.outcome == "expired_card_updated"
    assert after.used_at == _NOW and after.expires_at == row.expires_at
    async with expiry_sm.begin() as session:
        assert (
            await store.consume_credential_request(
                session, token=row.token, now=_NOW - timedelta(seconds=1)
            )
            is None
        ), "even a submit carrying an earlier clock cannot replay the retired token"
    again = await poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=edit)
    assert again.attempted == 0


@pytest.mark.parametrize("ack", [False, None, "truthy"])
async def test_unacknowledged_edits_retry_after_restart(expiry_sm, ack) -> None:
    row = await seed(expiry_sm)
    first = await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW, edit=AsyncMock(return_value=ack)
    )
    assert first.retired == 1 and first.acknowledged == 0
    assert (await read(expiry_sm, row.token)).outcome == "expired_card_pending"
    # New factory, no in-memory state or retained callback/cursor.
    restarted = async_sessionmaker(expiry_sm.kw["bind"], expire_on_commit=False)
    callback = AsyncMock(return_value=True)
    second = await poll_credential_form_expiries_once(restarted, now=_NOW, edit=callback)
    assert (second.retired, second.attempted, second.acknowledged) == (0, 1, 1)
    assert (await read(expiry_sm, row.token)).outcome == "expired_card_updated"
    assert callback.call_args.args[0].token == row.token


async def test_edit_without_ack_keeps_pending_and_advances_past_failure(expiry_sm) -> None:
    a = await seed(expiry_sm, token="a-expiry-request")
    b = await seed(expiry_sm, token="b-expiry-request")
    seen = []

    async def edit(row: CredentialRequestRow) -> bool:
        seen.append(row.token)
        if row.token == a.token:
            raise OSError("lost acknowledgement")
        return True

    first = await poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=edit, limit=1)
    second = await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW, edit=edit, limit=1, after_token=first.next_after_token
    )
    assert first.acknowledged == 0 and second.acknowledged == 1
    assert seen == [a.token, b.token]
    assert (await read(expiry_sm, a.token)).outcome == "expired_card_pending"
    assert (await read(expiry_sm, b.token)).outcome == "expired_card_updated"
    end = await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW, edit=edit, limit=1, after_token=second.next_after_token
    )
    assert end.attempted == 0 and end.next_after_token is None
    retry = AsyncMock(return_value=True)
    await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW, edit=retry, limit=1, after_token=end.next_after_token
    )
    assert retry.call_args.args[0].token == a.token


@pytest.mark.parametrize("change", ["identity", "outcome", "delete"])
async def test_ack_cas_cannot_retire_a_changed_or_deleted_card(expiry_sm, change) -> None:
    row = await seed(expiry_sm)

    async def edit(pending: CredentialRequestRow) -> bool:
        async with expiry_sm.begin() as session:
            if change == "identity":
                await store.update_credential_request_message(
                    session, token=pending.token, posted_message_id="999"
                )
            elif change == "outcome":
                await store.set_credential_request_outcome(
                    session, token=pending.token, outcome="write_failed"
                )
            else:
                await store.delete_credential_requests_for_platform_user(
                    session,
                    platform_user_id=pending.requester_platform_user_id,
                    tenant_id=pending.tenant_id,
                )
        return True

    result = await poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=edit)
    assert result.retired == 1 and result.acknowledged == 0
    if change != "delete":
        after = await read(expiry_sm, row.token)
        assert after.outcome == ("write_failed" if change == "outcome" else "expired_card_pending")


@pytest.mark.parametrize(
    "state",
    [
        "live",
        "slack",
        "no_platform",
        "no_thread",
        "no_message",
        "consumed",
        "replaced",
        "other_tenant",
    ],
)
async def test_ineligible_requests_are_never_edited(expiry_sm, state) -> None:
    fields = {}
    if state == "live":
        fields["expires_at"] = _NOW + timedelta(minutes=1)
    if state == "slack":
        fields["platform"] = "slack"
    if state == "no_platform":
        fields["platform"] = None
    if state == "no_thread":
        fields["origin_thread_id"] = None
    if state == "no_message":
        fields["posted_message_id"] = None
    row = await seed(expiry_sm, **fields)
    if state in ("consumed", "replaced"):
        async with expiry_sm.begin() as session:
            if state == "consumed":
                await store.consume_credential_request(
                    session, token=row.token, now=_NOW - timedelta(seconds=1)
                )
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome="applied"
                )
            else:
                await store.supersede_credential_request(session, token=row.token, now=_NOW)
    before = await read(expiry_sm, row.token)
    callback = AsyncMock(return_value=True)
    result = await poll_credential_form_expiries_once(
        expiry_sm,
        now=_NOW,
        edit=callback,
        tenant_id=uuid.uuid4() if state == "other_tenant" else row.tenant_id,
    )
    assert (result.retired, result.attempted, result.acknowledged) == (0, 0, 0)
    callback.assert_not_awaited()
    assert await read(expiry_sm, row.token) == before


@pytest.mark.parametrize("first", ["consume", "supersede", "expiry"])
async def test_consume_expire_race_is_serialized_by_the_real_row_lock(expiry_sm, first) -> None:
    row = await seed(expiry_sm)
    async with expiry_sm() as winner:
        async with winner.begin():
            if first == "consume":
                saved = await store.consume_credential_request(
                    winner, token=row.token, now=_NOW - timedelta(seconds=1)
                )
                assert saved is not None
            elif first == "supersede":
                saved = await store.supersede_credential_request(winner, token=row.token, now=_NOW)
                assert saved is not None
            else:
                assert await store.retire_expired_credential_requests(winner, now=_NOW) == 1
            if first == "expiry":

                async def consume():
                    async with expiry_sm.begin() as loser:
                        return await store.consume_credential_request(
                            loser, token=row.token, now=_NOW - timedelta(seconds=1)
                        )

                blocked = asyncio.create_task(consume())
                await asyncio.sleep(0.05)
                assert not blocked.done(), "the second UPDATE waits on the winner's row lock"
            else:
                async with expiry_sm.begin() as loser:
                    assert await store.retire_expired_credential_requests(loser, now=_NOW) == 0
        if first == "expiry":
            assert await asyncio.wait_for(blocked, timeout=5) is None
    after = await read(expiry_sm, row.token)
    assert (
        after.outcome
        == {"consume": None, "supersede": "replaced_by_newer", "expiry": "expired_card_pending"}[
            first
        ]
    )
    assert after.used_at == (_NOW - timedelta(seconds=1) if first == "consume" else _NOW)


async def test_concurrent_expiry_claims_retire_exactly_once(expiry_sm) -> None:
    row = await seed(expiry_sm)

    async def retire():
        async with expiry_sm.begin() as session:
            return await store.retire_expired_credential_requests(session, now=_NOW)

    counts = await asyncio.gather(retire(), retire())
    assert sorted(counts) == [0, 1]
    assert (await read(expiry_sm, row.token)).outcome == "expired_card_pending"


async def test_cancel_after_retirement_retains_pending_for_another_process(expiry_sm) -> None:
    row = await seed(expiry_sm)
    entered = asyncio.Event()

    async def stalled(pending: CredentialRequestRow) -> bool:
        entered.set()
        await asyncio.Event().wait()
        return True

    task = asyncio.create_task(
        poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=stalled)
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await read(expiry_sm, row.token)).outcome == "expired_card_pending"
    recovered = await poll_credential_form_expiries_once(
        expiry_sm, now=_NOW, edit=AsyncMock(return_value=True)
    )
    assert recovered.acknowledged == 1


@pytest.mark.parametrize("now,limit", [(_NOW.replace(tzinfo=None), 20), (_NOW, 0), (_NOW, 101)])
async def test_invalid_invocation_refuses_before_connecting(now, limit) -> None:
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    with pytest.raises(ValueError):
        await poll_credential_form_expiries_once(factory, now=now, limit=limit, edit=AsyncMock())
    factory.assert_not_called()
    factory.begin.assert_not_called()


async def test_edit_timeout_leaves_pending_and_continues_to_other_cards(expiry_sm, monkeypatch):
    from daimon.core import credential_form_expiry as job

    monkeypatch.setattr(job, "_EDIT_TIMEOUT_S", 0.05)
    rows = [await seed(expiry_sm), await seed(expiry_sm)]
    rows.sort(key=lambda row: row.token)
    cancelled = asyncio.Event()

    async def edit(row):
        if row.token == rows[0].token:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return True

    result = await poll_credential_form_expiries_once(expiry_sm, now=_NOW, edit=edit)
    assert (result.retired, result.attempted, result.acknowledged) == (2, 2, 1)
    assert cancelled.is_set()
    assert (await read(expiry_sm, rows[0].token)).outcome == "expired_card_pending"
    assert (await read(expiry_sm, rows[1].token)).outcome == "expired_card_updated"


async def test_stopped_owner_loop_constructs_no_connection_or_edit() -> None:
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    edit = AsyncMock(side_effect=AssertionError("must not edit"))
    await run_credential_form_expiry_poller(factory, edit=edit, should_stop=lambda: True)
    factory.assert_not_called()
    factory.begin.assert_not_called()
    edit.assert_not_awaited()


async def test_live_owner_loop_uses_real_job_and_stops_without_resuming_work(expiry_sm) -> None:
    row = await seed(expiry_sm, expires_at=_NOW - timedelta(days=1))
    stopped = False

    async def edit(pending):
        nonlocal stopped
        assert pending.token == row.token and pending.outcome == "expired_card_pending"
        assert (await read(expiry_sm, row.token)).used_at is not None
        stopped = True
        return True

    await run_credential_form_expiry_poller(
        expiry_sm, edit=edit, should_stop=lambda: stopped, interval_s=0
    )
    after = await read(expiry_sm, row.token)
    assert after.outcome == "expired_card_updated"
    assert after.requested_work == row.requested_work

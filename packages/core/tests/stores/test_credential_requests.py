"""Integration tests for the credential_requests store — real Postgres.

Covers the atomic single-use consume (the authoritative security gate) and
the platform-user-scoped erasure helpers.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import cast, get_args

import pytest
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
    CredentialRequestKind,
    mint_request_token,
)
from daimon.core.stores import credential_requests as store
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_credential_request = partial(
    store.create_credential_request,
    mcp_server_url=None,
    channel_id="chan-1",
    target_ma_agent_id="ag_test",
    target_name="tester",
    requested_work=None,
    requester_platform_user_id="requester-1",
)


async def _seed_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    token: str | None = None,
    requester_platform_user_id: str = "requester-1",
    expires_at: datetime | None = None,
    agent_id: uuid.UUID | None = None,
    origin_thread_id: str | None = None,
    platform: str | None = None,
) -> CredentialRequestRow:
    return await _credential_request(
        session,
        token=token or mint_request_token(),
        kind="env",
        tenant_id=tenant_id,
        agent_id=agent_id or uuid.uuid4(),
        account_id=account_id,
        target="OPENAI_API_KEY",
        requester_platform_user_id=requester_platform_user_id,
        expires_at=expires_at or (datetime.now(tz=UTC) + timedelta(minutes=30)),
        idempotency_key=uuid.uuid4(),
        origin_thread_id=origin_thread_id,
        platform=platform,
    )


async def test_create_credential_request_returns_row_with_used_at_none(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    row = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)

    assert row.used_at is None, "a freshly created request must be unused"


async def test_peek_credential_request_returns_row_for_unused_unexpired_token(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)

    peeked = await store.peek_credential_request(db_session, token=created.token)

    assert peeked is not None
    assert peeked.token == created.token


async def test_peek_credential_request_returns_row_even_when_expired(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
    )

    peeked = await store.peek_credential_request(db_session, token=created.token)

    assert peeked is not None, "peek must return an expired row so the caller can tell it apart"


async def test_peek_credential_request_returns_row_even_when_used(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)
    await store.consume_credential_request(
        db_session, token=created.token, now=datetime.now(tz=UTC)
    )

    peeked = await store.peek_credential_request(db_session, token=created.token)

    assert peeked is not None, "peek must return a used row so the caller can tell it apart"
    assert peeked.used_at is not None


async def test_peek_credential_request_returns_none_for_unknown_token(
    db_session: AsyncSession,
) -> None:
    peeked = await store.peek_credential_request(db_session, token=mint_request_token())

    assert peeked is None


async def test_consume_credential_request_sets_used_at_on_first_call(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)
    now = datetime.now(tz=UTC)

    consumed = await store.consume_credential_request(db_session, token=created.token, now=now)

    assert consumed is not None
    assert consumed.used_at == now


async def test_consume_credential_request_second_call_returns_none_and_preserves_used_at(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)
    first_now = datetime.now(tz=UTC)

    first = await store.consume_credential_request(db_session, token=created.token, now=first_now)
    second = await store.consume_credential_request(
        db_session, token=created.token, now=datetime.now(tz=UTC) + timedelta(seconds=5)
    )

    assert first is not None
    assert second is None, "a second consume of an already-used token must return None"

    peeked = await store.peek_credential_request(db_session, token=created.token)
    assert peeked is not None
    assert peeked.used_at == first_now, (
        "used_at must still hold the first call's timestamp, not be overwritten"
    )


async def test_consume_credential_request_returns_none_when_expired(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    created = await _seed_request(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
    )

    consumed = await store.consume_credential_request(
        db_session, token=created.token, now=datetime.now(tz=UTC)
    )

    assert consumed is None, "an expired token must not be consumable"

    peeked = await store.peek_credential_request(db_session, token=created.token)
    assert peeked is not None
    assert peeked.used_at is None, "a rejected consume must leave used_at NULL"


async def test_consume_credential_request_returns_none_for_unknown_token(
    db_session: AsyncSession,
) -> None:
    consumed = await store.consume_credential_request(
        db_session, token=mint_request_token(), now=datetime.now(tz=UTC)
    )

    assert consumed is None


async def test_delete_credential_requests_for_platform_user_deletes_regardless_of_state(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    unused = await _seed_request(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        requester_platform_user_id="erase-me",
    )
    used = await _seed_request(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        requester_platform_user_id="erase-me",
    )
    await store.consume_credential_request(db_session, token=used.token, now=datetime.now(tz=UTC))
    expired = await _seed_request(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        requester_platform_user_id="erase-me",
        expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
    )

    deleted = await store.delete_credential_requests_for_platform_user(
        db_session, platform_user_id="erase-me", tenant_id=tenant.id
    )

    assert deleted == 3, "delete must remove unused, used, AND expired rows"
    assert (await store.peek_credential_request(db_session, token=unused.token)) is None
    assert (await store.peek_credential_request(db_session, token=used.token)) is None
    assert (await store.peek_credential_request(db_session, token=expired.token)) is None


async def test_count_credential_requests_for_platform_user_matches_delete_rowcount(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    for _ in range(3):
        await _seed_request(
            db_session,
            tenant_id=tenant.id,
            account_id=account.id,
            requester_platform_user_id="count-me",
        )

    count_before = await store.count_credential_requests_for_platform_user(
        db_session, platform_user_id="count-me", tenant_id=tenant.id
    )
    deleted = await store.delete_credential_requests_for_platform_user(
        db_session, platform_user_id="count-me", tenant_id=tenant.id
    )

    assert count_before == deleted == 3


def _concurrency_dsn() -> str:
    """Read the real test DSN.

    The single-use guarantee rests on Postgres row locks serializing two
    *separate* connections, so this needs two independent engines rather than
    the shared single-connection ``db_session`` fixture the rest of this file
    uses.
    """
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the concurrency test")
    return url


async def test_concurrent_consume_of_one_token_succeeds_exactly_once() -> None:
    """Two connections racing the same token: one wins, one gets None.

    The consume is a single `UPDATE ... WHERE used_at IS NULL AND
    expires_at > now RETURNING`, so the loser's WHERE clause matches zero rows
    once the winner's row lock is released. Asserting that empirically is the
    difference between trusting the SQL shape and knowing it holds — and this
    is the only gate standing between a leaked button and a second credential
    write.

    Runs against the default schema with a freshly minted token, so parallel
    pytest workers cannot collide.
    """
    dsn = _concurrency_dsn()
    engine_a = create_async_engine(dsn)
    engine_b = create_async_engine(dsn)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)

    token = mint_request_token()
    try:
        async with factory_a.begin() as seed:
            tenant = await make_tenant(seed)
            account = await make_account(seed, tenant=tenant)
            await _seed_request(seed, tenant_id=tenant.id, account_id=account.id, token=token)

        now = datetime.now(tz=UTC)

        async def consume(factory: async_sessionmaker[AsyncSession]) -> CredentialRequestRow | None:
            async with factory.begin() as session:
                return await store.consume_credential_request(session, token=token, now=now)

        first, second = await asyncio.gather(consume(factory_a), consume(factory_b))

        winners = [row for row in (first, second) if row is not None]
        assert len(winners) == 1, (
            "exactly one racing connection may consume a single-use credential request; "
            f"got {len(winners)} winners"
        )
        assert winners[0].token == token, "the winning consume must return the raced row"

        async with factory_a() as check:
            after = await store.peek_credential_request(check, token=token)
        assert after is not None, "the row must survive consumption (it is marked, not deleted)"
        assert after.used_at is not None, "the winning consume must persist used_at"

        replayed = await consume(factory_a)
        assert replayed is None, "a consumed token must never be consumable again"
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


async def test_credential_requests_rejects_unknown_kind(db_session: AsyncSession) -> None:
    """The CHECK is the durable half of the Literal: a kind nobody declared cannot land."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    with pytest.raises(IntegrityError):
        await _credential_request(
            db_session,
            token=mint_request_token(),
            kind=cast(CredentialRequestKind, "telepathy"),
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            target="OPENAI_API_KEY",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
        )
    await db_session.rollback()


async def test_credential_requests_accepts_every_declared_kind(db_session: AsyncSession) -> None:
    """Every kind the Literal declares must survive the CHECK — no dead vocabulary."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    kinds = get_args(CredentialRequestKind)
    assert kinds, "CredentialRequestKind must declare at least one kind"
    for kind in kinds:
        row = await _credential_request(
            db_session,
            token=mint_request_token(),
            kind=kind,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            target=ENV_FILE_TARGET if kind == "env_file" else "TARGET",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
        )
        assert row.kind == kind, f"kind {kind} must round-trip through the store"


async def test_credential_requests_idempotency_key_is_unique(db_session: AsyncSession) -> None:
    """One idempotency key names one control; a re-post must collide, not stack."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    shared_key = uuid.uuid4()

    await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)
    await _credential_request(
        db_session,
        token=mint_request_token(),
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="A",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
        idempotency_key=shared_key,
    )

    with pytest.raises(IntegrityError):
        await _credential_request(
            db_session,
            token=mint_request_token(),
            kind="env",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            target="B",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
            idempotency_key=shared_key,
        )
    await db_session.rollback()


async def test_create_credential_request_round_trips_its_provenance(
    db_session: AsyncSession,
) -> None:
    """The provenance the mint site states must be readable back off the row."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    replaces = datetime.now(tz=UTC) - timedelta(minutes=5)
    key = uuid.uuid4()

    row = await _credential_request(
        db_session,
        token=mint_request_token(),
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="OPENAI_API_KEY",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
        idempotency_key=key,
        target_ma_agent_id="ag_target",
        target_name="research-bot",
        requested_work="pull last week's Toggl hours",
        responder_name="daimon",
        replaces_updated_at=replaces,
    )

    assert row.idempotency_key == key, "idempotency_key must round-trip"
    assert row.target_ma_agent_id == "ag_target", "target_ma_agent_id must round-trip"
    assert row.target_name == "research-bot", "target_name must round-trip"
    assert row.requested_work == "pull last week's Toggl hours", "requested_work must round-trip"
    assert row.responder_name == "daimon", "responder_name must round-trip"
    assert row.replaces_updated_at == replaces, "replaces_updated_at must round-trip"
    assert row.outcome is None, "a freshly minted control has no outcome yet"


async def test_set_credential_request_outcome_records_the_outcome(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)

    await store.set_credential_request_outcome(db_session, token=row.token, outcome="applied")

    after = await store.peek_credential_request(db_session, token=row.token)
    assert after is not None, "the row must still exist after recording its outcome"
    assert after.outcome == "applied", "the recorded outcome must be readable back"


async def test_set_credential_request_outcome_is_last_write_wins_and_idempotent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)

    await store.set_credential_request_outcome(db_session, token=row.token, outcome="write_failed")
    await store.set_credential_request_outcome(
        db_session, token=row.token, outcome="stale_replacement"
    )

    after = await store.peek_credential_request(db_session, token=row.token)
    assert after is not None, "the row must still exist"
    assert after.outcome == "stale_replacement", "the last recorded outcome must win"


async def test_set_credential_request_outcome_does_not_raise_for_unknown_token(
    db_session: AsyncSession,
) -> None:
    """No raise if absent — the outcome is a trace, never a gate."""
    await store.set_credential_request_outcome(db_session, token="no-such-token", outcome="applied")


async def test_list_live_credential_requests_excludes_used_and_expired(
    db_session: AsyncSession,
) -> None:
    """Live means what the consume gate accepts, inside one supersede scope."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    now = datetime.now(tz=UTC)

    async def seed(**overrides: object) -> CredentialRequestRow:
        kwargs: dict[str, object] = {
            "tenant_id": tenant.id,
            "account_id": account.id,
            "agent_id": agent_id,
            "origin_thread_id": "thread-1",
        }
        kwargs.update(overrides)
        return await _seed_request(db_session, **kwargs)  # type: ignore[arg-type]

    live = await seed()
    used = await seed()
    await store.consume_credential_request(db_session, token=used.token, now=now)
    expired = await seed(expires_at=now - timedelta(minutes=1))
    other_thread = await seed(origin_thread_id="thread-2")
    other_agent = await seed(agent_id=uuid.uuid4())
    other_requester = await seed(requester_platform_user_id="requester-2")

    found = await store.list_live_credential_requests(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        requester_platform_user_id="requester-1",
        origin_thread_id="thread-1",
        now=now,
    )

    assert [row.token for row in found] == [live.token], (
        "only the unused, unexpired request for this requester, thread and agent is live"
    )
    for row, why in (
        (used, "a consumed request"),
        (expired, "an expired request"),
        (other_thread, "a request in another thread"),
        (other_agent, "a request for another agent"),
        (other_requester, "another person's request"),
    ):
        assert row.token not in {found_row.token for found_row in found}, (
            f"{why} must never be listed as live here"
        )


async def test_supersede_marks_used_with_replaced_outcome_once(
    db_session: AsyncSession,
) -> None:
    """The retirement spends the row exactly like a click would, and only once."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _seed_request(db_session, tenant_id=tenant.id, account_id=account.id)
    now = datetime.now(tz=UTC)

    first = await store.supersede_credential_request(db_session, token=row.token, now=now)
    second = await store.supersede_credential_request(
        db_session, token=row.token, now=now + timedelta(seconds=1)
    )

    assert first is not None, "an unused request must be retirable"
    assert first.used_at == now, "retiring spends the row at the supplied time"
    assert first.outcome == "replaced_by_newer", "the row records why it was never clicked"
    assert second is None, "a row already spent must not be retired a second time"
    after = await store.peek_credential_request(db_session, token=row.token)
    assert after is not None and after.used_at == now, "the second call must change nothing"
    assert (
        await store.consume_credential_request(
            db_session, token=row.token, now=now + timedelta(seconds=2)
        )
        is None
    ), "a retired request can no longer be consumed by a late click"


async def test_expire_unclicked_spends_only_posted_past_deadline_rows_on_the_platform_once(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(tz=UTC)
    past = now - timedelta(minutes=1)

    async def seed(*, platform: str, expires_at: datetime, posted: bool) -> CredentialRequestRow:
        row = await _seed_request(
            db_session,
            tenant_id=tenant.id,
            account_id=account.id,
            expires_at=expires_at,
            origin_thread_id="thread-1",
            platform=platform,
        )
        if posted:
            await store.update_credential_request_message(
                db_session, token=row.token, posted_message_id="msg-1"
            )
        return row

    due = await seed(platform="discord", expires_at=past, posted=True)
    live = await seed(platform="discord", expires_at=now + timedelta(minutes=5), posted=True)
    never_posted = await seed(platform="discord", expires_at=past, posted=False)
    other_platform = await seed(platform="slack", expires_at=past, posted=True)
    clicked = await seed(platform="discord", expires_at=now + timedelta(seconds=1), posted=True)
    assert await store.consume_credential_request(db_session, token=clicked.token, now=now)
    later = now + timedelta(minutes=1)

    first = await store.expire_unclicked_credential_requests(
        db_session, platform="discord", now=later
    )
    second = await store.expire_unclicked_credential_requests(
        db_session, platform="discord", now=later
    )

    assert [row.token for row in first] == [due.token]
    assert first[0].used_at == later and first[0].outcome == "expired"
    assert second == [], "a retired form is retired once"
    for untouched in (live, never_posted, other_platform):
        after = await store.peek_credential_request(db_session, token=untouched.token)
        assert after is not None and after.used_at is None and after.outcome is None
    after_click = await store.peek_credential_request(db_session, token=clicked.token)
    assert after_click is not None and after_click.outcome is None, "a clicked form is not expired"


async def test_expiry_sweep_and_submit_never_both_spend_one_form() -> None:
    """Separate connections, both lock orders, and two sweepers at once.

    A submit holding the row (uncommitted) is skipped by the sweep and keeps
    its own outcome; a sweep holding the row makes a waiting submit return
    None; two sweepers claim disjoint rows. Scoped to a fresh platform name
    so parallel workers' rows are never swept here.
    """
    dsn = _concurrency_dsn()
    engines = [create_async_engine(dsn) for _ in range(2)]
    a, b = (async_sessionmaker(engine, expire_on_commit=False) for engine in engines)
    platform = f"race-{uuid.uuid4()}"
    deadline = datetime.now(tz=UTC) + timedelta(minutes=30)
    before, after = deadline - timedelta(seconds=1), deadline + timedelta(minutes=1)

    async def posted_form() -> str:
        async with a.begin() as seed:
            tenant = await make_tenant(seed)
            account = await make_account(seed, tenant=tenant)
            row = await _seed_request(
                seed,
                tenant_id=tenant.id,
                account_id=account.id,
                expires_at=deadline,
                platform=platform,
            )
            await store.update_credential_request_message(
                seed, token=row.token, posted_message_id="msg"
            )
        return row.token

    try:
        submitting = await posted_form()
        async with a.begin() as submit:
            assert await store.consume_credential_request(submit, token=submitting, now=before)
            async with b.begin() as sweep:
                assert (
                    await store.expire_unclicked_credential_requests(
                        sweep, platform=platform, now=after
                    )
                    == []
                ), "a form being submitted is skipped, not waited on"
            await store.set_credential_request_outcome(submit, token=submitting, outcome="applied")
        async with b.begin() as sweep:
            assert (
                await store.expire_unclicked_credential_requests(
                    sweep, platform=platform, now=after
                )
                == []
            )
        async with a() as check:
            submitted = await store.peek_credential_request(check, token=submitting)
        assert submitted is not None and submitted.outcome == "applied"

        expiring = await posted_form()
        async with b.begin() as sweep:
            claimed = await store.expire_unclicked_credential_requests(
                sweep, platform=platform, now=after
            )
            assert [row.token for row in claimed] == [expiring]

            async def late_submit() -> CredentialRequestRow | None:
                async with a.begin() as submit:
                    return await store.consume_credential_request(
                        submit, token=expiring, now=before
                    )

            waiting = asyncio.create_task(late_submit())
            await asyncio.sleep(0.2)
            assert not waiting.done(), "the submit waits on the sweep's row lock"
        assert await waiting is None, "a submit that loses to the sweep spends nothing"

        first, second = await posted_form(), await posted_form()
        async with a.begin() as sweep_a, b.begin() as sweep_b:
            got_a = await store.expire_unclicked_credential_requests(
                sweep_a, platform=platform, now=after, limit=1
            )
            got_b = await store.expire_unclicked_credential_requests(
                sweep_b, platform=platform, now=after, limit=1
            )
        assert {got_a[0].token, got_b[0].token} == {first, second}
    finally:
        for engine in engines:
            await engine.dispose()

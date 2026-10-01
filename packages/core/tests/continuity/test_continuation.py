"""The continuation dispatch contract: queue once, claim once, and know when not to run.

Real Postgres throughout — the at-most-once guarantee is a row lock, not a
Python flag — and a transport-level fake MA, because re-resolving the concrete
destination is one of the decisions under test.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from daimon.core.continuity.continuation import (
    MAX_REQUESTED_WORK,
    MIN_REQUESTED_WORK,
    ContinuationRequest,
    build_input_continuation,
    claim_continuation,
    decide_continuation,
    record_continuation,
    sanitize_requested_work,
    settle_continuation,
)
from daimon.core.errors import DaimonError
from daimon.core.stores.domain import ContinuationReason, CredentialRequestRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, not_found_response
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_TARGET_ID = "agt_stats"


@pytest_asyncio.fixture
async def caller(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A tenant and one account inside it, committed and ready to be referenced."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return tenant.id, account.id


def _request(
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    requested_work: str | None = "finish the churn writeup",
    target_ma_agent_id: str = _TARGET_ID,
    reason: ContinuationReason = "task_handoff",
) -> ContinuationRequest:
    return ContinuationRequest(
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        requester_account_id=account_id,
        requester_external_user_id="discord-user-1",
        target_ma_agent_id=target_ma_agent_id,
        target_name="stats-bot",
        requested_work=requested_work,
        reason=reason,
        idempotency_key=uuid.uuid4(),
    )


def _credential_request_row(
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    origin_thread_id: str | None = "T_THREAD",
    parent_channel_id: str | None = "C_PARENT",
    target_ma_agent_id: str | None = _TARGET_ID,
    target_name: str | None = "stats-bot",
    requested_work: str | None = "finish the churn writeup",
    idempotency_key: uuid.UUID | None = None,
) -> CredentialRequestRow:
    """A consumed private-input request, as the store hands it back."""
    return CredentialRequestRow(
        token="tok-1",
        kind="agent_key",
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=account_id,
        target="stats-bot",
        mcp_server_url=None,
        requester_platform_user_id="discord-user-1",
        channel_id="C_FALLBACK",
        platform="discord",
        parent_channel_id=parent_channel_id,
        origin_thread_id=origin_thread_id,
        idempotency_key=uuid.uuid4() if idempotency_key is None else idempotency_key,
        requested_work=requested_work,
        target_ma_agent_id=target_ma_agent_id,
        target_name=target_name,
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        used_at=_NOW,
    )


def _live_target(tenant_id: uuid.UUID) -> AsyncAnthropic:
    agent = ma_agent(
        id=_TARGET_ID,
        name="stats-bot",
        model="claude-sonnet-5",
        tenant_id=tenant_id,
        created_at=_NOW,
    )
    router = MARouter()
    router.add_agent(agent)
    return build_fake_anthropic(router.dispatch)


def _missing_target() -> AsyncAnthropic:
    router = MARouter()
    router.add(
        "GET", rf"/v1/agents/{_TARGET_ID}", lambda _r, _m: not_found_response("agent not found")
    )
    return build_fake_anthropic(router.dispatch)


def _archived_target(tenant_id: uuid.UUID) -> AsyncAnthropic:
    archived = ma_agent(
        id=_TARGET_ID,
        name="stats-bot",
        model="claude-sonnet-5",
        tenant_id=tenant_id,
        created_at=_NOW,
        archived_at=_NOW,
    )
    router = MARouter()
    router.add_agent(archived)
    return build_fake_anthropic(router.dispatch)


async def test_decide_skips_save_only_when_the_handoff_carried_no_work(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A switch alone promised nothing, so it must never spend a billed turn."""
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id, requested_work=None)
    await record_continuation(db_session_factory, request)

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=None,
        active_turn=False,
    )

    assert decision.action == "skip_save_only", "no requested work means no dispatch"
    assert decision.message is None, "a silent skip says nothing to the person"
    assert decision.seed_user_message is None, "nothing is seeded when nothing runs"


async def test_decide_dispatches_with_the_requested_work_as_the_seed_message(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, request)

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=None,
        active_turn=False,
    )

    assert decision.action == "dispatch", "a live target with work queued runs the turn"
    assert decision.seed_user_message == "finish the churn writeup", (
        "the person's own words are what the new agent is asked to continue"
    )
    assert decision.message is None, "a dispatch needs no skip copy"


@pytest.mark.parametrize("target_state", ["missing", "archived", "other_tenant"])
async def test_decide_skips_when_the_target_is_no_longer_that_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    target_state: str,
) -> None:
    """Never substitute a namesake: a changed destination is a skip, not a guess."""
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, request)
    if target_state == "missing":
        anthropic = _missing_target()
    elif target_state == "archived":
        anthropic = _archived_target(tenant_id)
    else:
        anthropic = _live_target(uuid.uuid4())

    decision = await decide_continuation(
        db_session_factory,
        anthropic,
        request=request,
        now=_NOW,
        latest_user_message_at=None,
        active_turn=False,
    )

    assert decision.action == "skip_target_changed", (
        f"a {target_state} target must not receive a queued turn"
    )
    assert decision.message is not None, "the person is told the target changed"
    assert "stats-bot is no longer the agent this was set up for." in decision.message, (
        "the copy names the agent the person chose"
    )
    assert "Nothing was lost." in decision.message, "the copy must not imply the work was destroyed"


async def test_decide_skips_when_the_person_has_spoken_again_since_queueing(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, request)
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=request.idempotency_key)
    assert row is not None, "the recorded row is the clock the supersede rule reads"

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=row.created_at + timedelta(seconds=1),
        active_turn=False,
    )

    assert decision.action == "skip_superseded", "a newer message replaces the queued work"


async def test_decide_dispatches_when_the_last_message_predates_the_queued_work(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The boundary the other way: the message that asked for the handoff is older."""
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, request)
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=request.idempotency_key)
    assert row is not None, "the recorded row must exist to compare against"

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=row.created_at - timedelta(seconds=1),
        active_turn=False,
    )

    assert decision.action == "dispatch", "the request that caused the handoff cannot supersede it"


async def test_decide_defers_while_a_turn_is_already_running_in_the_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, request)

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=None,
        active_turn=True,
    )

    assert decision.action == "skip_turn_running", "the in-flight turn finishes first"
    assert decision.message is not None and "stats-bot takes over from your next message here." in (
        decision.message
    ), "the person is told who answers next and that the current message finishes as it is"


async def test_decide_refuses_a_request_that_was_never_recorded(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Deciding without a row would compare against a clock that does not exist."""
    tenant_id, account_id = caller

    with pytest.raises(DaimonError, match="never recorded"):
        await decide_continuation(
            db_session_factory,
            _live_target(tenant_id),
            request=_request(tenant_id=tenant_id, account_id=account_id),
            now=_NOW,
            latest_user_message_at=None,
            active_turn=False,
        )


async def test_settle_records_delivery_and_skip_distinctly(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    delivered = _request(tenant_id=tenant_id, account_id=account_id)
    skipped = _request(tenant_id=tenant_id, account_id=account_id)
    await record_continuation(db_session_factory, delivered)
    await record_continuation(db_session_factory, skipped)

    await claim_continuation(
        db_session_factory, idempotency_key=delivered.idempotency_key, now=_NOW
    )
    await claim_continuation(db_session_factory, idempotency_key=skipped.idempotency_key, now=_NOW)
    await settle_continuation(
        db_session_factory,
        idempotency_key=delivered.idempotency_key,
        status="delivered",
        now=_NOW,
    )
    await settle_continuation(
        db_session_factory,
        idempotency_key=skipped.idempotency_key,
        status="skipped",
        now=_NOW,
        skip_reason="skip_superseded",
    )

    async with db_session_factory() as session:
        delivered_row = await get_continuation(session, idempotency_key=delivered.idempotency_key)
        skipped_row = await get_continuation(session, idempotency_key=skipped.idempotency_key)
    assert delivered_row is not None and delivered_row.delivered_at == _NOW, (
        "only a delivered continuation carries a delivery time"
    )
    assert skipped_row is not None and skipped_row.delivered_at is None, (
        "a skipped row must never read as though a turn ran for it"
    )
    assert skipped_row.skip_reason == "skip_superseded", "the skip records why it was skipped"


def _concurrency_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for the concurrency test")
    return url


async def test_two_dispatchers_racing_one_continuation_claim_it_exactly_once() -> None:
    """Two adapter processes, one queued turn: the database picks the single winner.

    Two real engines, because the guarantee is a Postgres row lock serializing
    separate connections — a shared-connection fixture cannot demonstrate it.
    Runs in the default schema with a freshly minted key so parallel pytest
    workers cannot collide.
    """
    dsn = _concurrency_dsn()
    engine_a = create_async_engine(dsn)
    engine_b = create_async_engine(dsn)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)

    try:
        async with factory_a.begin() as seed:
            tenant = await make_tenant(seed)
            account = await make_account(seed, tenant=tenant)
        request = _request(tenant_id=tenant.id, account_id=account.id)
        await record_continuation(factory_a, request)

        first, second = await asyncio.gather(
            claim_continuation(factory_a, idempotency_key=request.idempotency_key, now=_NOW),
            claim_continuation(factory_b, idempotency_key=request.idempotency_key, now=_NOW),
        )

        assert [first, second].count(True) == 1, (
            "exactly one dispatcher may claim a continuation; "
            f"got {[first, second].count(True)} winners"
        )
        async with factory_a() as session:
            row = await get_continuation(session, idempotency_key=request.idempotency_key)
        assert row is not None and row.status == "claimed", "the winner's claim is committed"
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (None, "a null continuation stays null"),
        ("", "an empty string describes no work"),
        ("   ", "whitespace describes no work"),
        ("go", f"anything shorter than {MIN_REQUESTED_WORK} characters cannot describe work"),
        ("stats-bot", "the destination's own name is an echo, not a request"),
        ("  STATS-BOT  ", "the echo check is case-folded and stripped"),
    ],
)
def test_sanitize_requested_work_nulls_empty_short_and_echoes(text: str | None, why: str) -> None:
    assert sanitize_requested_work(text, echoes=("stats-bot",)) is None, why


def test_sanitize_requested_work_keeps_real_text_untruncated() -> None:
    """Sanitising is a null-or-keep decision; slicing to the cap is the caller's job."""
    long_text = "finish the churn writeup " * 40
    assert len(long_text) > MAX_REQUESTED_WORK, "the fixture must exceed the cap to be a real test"

    kept = sanitize_requested_work(long_text, echoes=("stats-bot",))

    assert kept == long_text, "real work is returned byte-for-byte, neither trimmed nor truncated"


def test_build_input_continuation_returns_none_for_a_pre_phase_row(
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A row without a frozen target would have to re-resolve an agent by name."""
    tenant_id, account_id = caller
    row = _credential_request_row(
        tenant_id=tenant_id, account_id=account_id, target_ma_agent_id=None
    )

    assert build_input_continuation(row, platform="discord") is None, (
        "a row predating the frozen-target columns owes no continuation"
    )


def test_build_input_continuation_returns_none_without_origin_thread(
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    row = _credential_request_row(tenant_id=tenant_id, account_id=account_id, origin_thread_id=None)

    assert build_input_continuation(row, platform="discord") is None, (
        "there is nowhere to post a continuation for a row with no origin thread"
    )


def test_build_input_continuation_reuses_the_persisted_idempotency_key(
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The key is the at-most-once gate: a fresh one would allow a second turn."""
    tenant_id, account_id = caller
    key = uuid.uuid4()
    row = _credential_request_row(tenant_id=tenant_id, account_id=account_id, idempotency_key=key)

    request = build_input_continuation(row, platform="discord")

    assert request is not None, "a complete row owes a continuation"
    assert request.idempotency_key == key, (
        "the continuation must carry the row's persisted key, not a newly minted one"
    )


def test_build_input_continuation_maps_origin_and_requester(
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    row = _credential_request_row(tenant_id=tenant_id, account_id=account_id)

    request = build_input_continuation(row, platform="slack")

    assert request is not None, "a complete row owes a continuation"
    assert request.platform == "slack", "the platform is the caller's, not the row's stored string"
    assert request.thread_id == "T_THREAD", "the continuation posts into the originating thread"
    assert request.parent_channel_id == "C_PARENT", "the origin's parent channel wins when present"
    assert request.requester_account_id == account_id, "the requester is the row's account"
    assert request.requester_external_user_id == "discord-user-1", (
        "the platform identity rides across so the continuation runs as the same person"
    )
    assert request.target_ma_agent_id == _TARGET_ID, "the frozen concrete target is carried through"
    assert request.target_name == "stats-bot", "the frozen name is carried through for the copy"
    assert request.requested_work == "finish the churn writeup", (
        "the work the person asked for is what the continuation seeds"
    )
    assert request.reason == "private_input_applied", (
        "a consumed private-input request is a distinct producer from a handoff"
    )


def test_build_input_continuation_falls_back_to_the_rows_channel(
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, account_id = caller
    row = _credential_request_row(
        tenant_id=tenant_id, account_id=account_id, parent_channel_id=None
    )

    request = build_input_continuation(row, platform="discord")

    assert request is not None, "a missing parent channel is not a reason to owe nothing"
    assert request.parent_channel_id == "C_FALLBACK", (
        "a row with no recorded parent channel falls back to the channel it was posted in"
    )


async def test_decide_continuation_turn_running_copy_is_not_handoff_shaped_for_private_input(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Nobody took over: the key was saved for the agent already answering here."""
    tenant_id, account_id = caller
    request = _request(tenant_id=tenant_id, account_id=account_id, reason="private_input_applied")
    await record_continuation(db_session_factory, request)

    decision = await decide_continuation(
        db_session_factory,
        _live_target(tenant_id),
        request=request,
        now=_NOW,
        latest_user_message_at=None,
        active_turn=True,
    )

    assert decision.action == "skip_turn_running", "the in-flight turn still finishes first"
    assert decision.message is not None, "the person is told why their work waits"
    assert "takes over" not in decision.message, (
        "a private-input continuation changed nobody, so the copy must not announce a takeover"
    )
    assert "stats-bot is still working on the previous message here." in decision.message, (
        "the non-handoff copy names the agent that is already answering"
    )

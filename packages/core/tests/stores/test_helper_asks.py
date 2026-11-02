"""Helper asks: one running ask per helper, finished once, stopped with the asking turn."""

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import helper_asks
from daimon.core.stores.domain import HelperAskRow, Role, TurnOriginRow
from daimon.core.stores.turn_origins import create_origin, get_active_origin, mark_serving_helpers
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def _origin(session: AsyncSession, helper_of: uuid.UUID | None = None) -> TurnOriginRow:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    now = datetime.now(UTC)
    return await create_origin(
        session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="agent_lead",
        responder_name="lead",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        helper_of=helper_of,
    )


async def _ask(session: AsyncSession, origin: TurnOriginRow, helper: str, at: int) -> HelperAskRow:
    return await helper_asks.create_ask(
        session,
        tenant_id=origin.tenant_id,
        origin_id=origin.id,
        helper_agent_id=helper,
        helper_name=helper,
        message="check it",
        now=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=at),
    )


async def test_claims_the_oldest_ask_of_each_idle_helper(db_session: AsyncSession) -> None:
    origin = await _origin(db_session)
    first = await _ask(db_session, origin, "agent_a", 0)
    await _ask(db_session, origin, "agent_a", 1)
    other = await _ask(db_session, origin, "agent_b", 2)
    await _ask(db_session, origin, "agent_c", 3)

    claimed = await helper_asks.claim_queued(
        db_session, origin_id=origin.id, skip_helpers=(), limit=2
    )

    assert [ask.id for ask in claimed] == [first.id, other.id]
    assert {ask.status for ask in claimed} == {"running"}
    busy = await helper_asks.claim_queued(
        db_session, origin_id=origin.id, skip_helpers={"agent_a", "agent_b"}, limit=3
    )
    assert [ask.helper_agent_id for ask in busy] == ["agent_c"], "a busy helper's next ask waits"


async def test_an_ask_finishes_once_and_the_turn_end_stops_the_rest(
    db_session: AsyncSession,
) -> None:
    origin = await _origin(db_session)
    done = await _ask(db_session, origin, "agent_a", 0)
    open_ = await _ask(db_session, origin, "agent_b", 1)
    now = datetime.now(UTC)

    await helper_asks.finish_ask(db_session, ask_id=done.id, status="done", reply="ok", now=now)
    await helper_asks.stop_open_asks(db_session, origin_id=origin.id, now=now)
    await helper_asks.finish_ask(db_session, ask_id=open_.id, status="done", reply="late", now=now)

    asks = await helper_asks.list_asks(db_session, tenant_id=origin.tenant_id, origin_id=origin.id)
    assert [(a.status, a.reply) for a in asks] == [("done", "ok"), ("stopped", None)]
    assert await helper_asks.count_asks(db_session, origin_id=origin.id) == 2


async def test_asks_are_read_only_by_their_own_turn(db_session: AsyncSession) -> None:
    mine = await _origin(db_session)
    theirs = await _origin(db_session)
    ask = await _ask(db_session, theirs, "agent_a", 0)

    assert (
        await helper_asks.list_asks(
            db_session, tenant_id=mine.tenant_id, origin_id=mine.id, ids=[ask.id]
        )
        == []
    )
    assert (
        await helper_asks.list_asks(
            db_session, tenant_id=mine.tenant_id, origin_id=theirs.id, ids=[ask.id]
        )
        == []
    ), "another tenant's turn is never read"


async def test_an_origin_records_helper_turns_and_serving_turns(db_session: AsyncSession) -> None:
    asking = await _origin(db_session)
    helper = await _origin(db_session, helper_of=asking.id)
    await mark_serving_helpers(db_session, origin_id=asking.id)

    read = await get_active_origin(
        db_session,
        origin_id=asking.id,
        tenant_id=asking.tenant_id,
        account_id=asking.account_id,
        platform="slack",
        now=datetime.now(UTC),
    )
    assert read is not None and read.serves_helpers and read.helper_of is None
    assert helper.helper_of == asking.id and not helper.serves_helpers

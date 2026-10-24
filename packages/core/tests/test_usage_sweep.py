"""Tests for daimon.core.usage_sweep: the one path that settles a session's usage.

`settle_session` debits every model call a session has no ledger row for, at
the chat path's price and key, idempotently. The chat turn end, the MCP reads
that see a headless turn end, the scheduler drain and the operator's backlog
settle all go through it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import anthropic
import httpx
import pytest
import structlog.testing
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsSpanModelRequestEndEvent,
    BetaManagedAgentsTextBlock,
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core import usage_sweep
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import session_settle_state, turn_outcomes
from daimon.core.tenant_balance import debit_amount
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn.prepare import bind_recorder
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_sweep import (
    session_billing,
    settle_backlog,
    settle_finished_turn,
    settle_session,
    settle_stamped_session,
    sweep_headless_usage,
    turn_ended,
)
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, not_found_response
from mux.testing.drivers.anthropic.ma_models import ma_model_usage, ma_session, ma_session_agent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
MODEL = "claude-sonnet-4-6"
MARKUP = Decimal("1.1")
SESSION = "sesn_settle"
BOUNDARY = "evt_boundary"


def _call(event_id: str, *, at: datetime, input_tokens: int = 1000) -> dict[str, Any]:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id=f"start_{event_id}",
        model_usage=ma_model_usage(
            input_tokens=input_tokens, output_tokens=500, cache_read_input_tokens=200
        ),
        processed_at=at,
        type="span.model_request_end",
    ).model_dump(mode="json")


def _message(event_id: str, *, at: datetime) -> dict[str, Any]:
    return BetaManagedAgentsUserMessageEvent(
        id=event_id,
        content=[BetaManagedAgentsTextBlock(type="text", text="hi")],
        type="user.message",
        processed_at=at,
    ).model_dump(mode="json")


def _idle(event_id: str, *, at: datetime) -> dict[str, Any]:
    return BetaManagedAgentsSessionStatusIdleEvent(
        id=event_id,
        processed_at=at,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        type="session.status_idle",
    ).model_dump(mode="json")


def _session(
    tenant_id: uuid.UUID,
    *,
    session_id: str = SESSION,
    account_id: uuid.UUID | str | None = None,
    status: Any = "idle",
    exempt: bool = False,
    budget_channel: str | None = "chan-1",
    origin_channel: str | None = None,
) -> BetaManagedAgentsSession:
    metadata = {MA_METADATA_KEY_TENANT: str(tenant_id)}
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    if budget_channel is not None:
        metadata[MA_METADATA_KEY_BUDGET_CHANNEL] = budget_channel
    if origin_channel is not None:
        metadata[MA_METADATA_KEY_CHANNEL] = origin_channel
    if exempt:
        metadata[MA_METADATA_KEY_BILLING_EXEMPT] = "mcp-internal-caller"
    return ma_session(
        id=session_id,
        agent=ma_session_agent(model=MODEL),
        status=status,
        metadata=metadata,
    )


class _MA:
    """Sessions and their events, listed as MA lists them: `processed_at` order
    (`order=desc` reverses it), `types[]` and `created_at[gte|lt]` filters on
    `processed_at`, cursor pages. Records every events listing."""

    def __init__(self, *sessions: BetaManagedAgentsSession, stall_s: float = 0.0) -> None:
        self.sessions = {s.id: s for s in sessions}
        self.events: dict[str, list[dict[str, Any]]] = {s.id: [] for s in sessions}
        self.listings: list[httpx.QueryParams] = []
        self.stall_s = stall_s
        router = MARouter()
        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events", cast(Any, self._list))
        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)", cast(Any, self._retrieve))
        self.client = build_fake_anthropic(router.dispatch)

    def _retrieve(self, _request: httpx.Request, match: Any) -> httpx.Response:
        session = self.sessions.get(match["sid"])
        if session is None:
            return not_found_response("no such session")
        return httpx.Response(200, json=session.model_dump(mode="json"))

    async def _list(self, request: httpx.Request, match: Any) -> httpx.Response:
        if self.stall_s:
            await asyncio.sleep(self.stall_s)
        params = request.url.params
        self.listings.append(params)
        types = set(params.get_list("types[]"))
        gte, lt = params.get("created_at[gte]"), params.get("created_at[lt]")
        floor = datetime.fromisoformat(gte) if gte else None
        ceiling = datetime.fromisoformat(lt) if lt else None
        listed = sorted(
            (
                e
                for e in self.events.get(match["sid"], [])
                if (not types or e["type"] in types)
                and (floor is None or datetime.fromisoformat(e["processed_at"]) >= floor)
                and (ceiling is None or datetime.fromisoformat(e["processed_at"]) < ceiling)
            ),
            key=lambda e: e["processed_at"],
            reverse=params.get("order") == "desc",
        )
        start = int(params.get("page") or 0)
        end = start + int(params.get("limit") or 100)
        return httpx.Response(
            200,
            json={"data": listed[start:end], "next_page": str(end) if end < len(listed) else None},
        )

    def turn(self, *calls: str, session_id: str = SESSION, at: datetime = NOW) -> None:
        """A turn MA ran to its end: its message, its calls, then idle."""
        events = self.events[session_id]
        events.append(_message(BOUNDARY, at=at))
        for i, call in enumerate(calls, start=1):
            events.append(_call(call, at=at + timedelta(seconds=i), input_tokens=1000 * i))
        events.append(_idle(f"idle_{len(events)}", at=at + timedelta(seconds=len(calls) + 1)))


async def _ledger(db_session: AsyncSession) -> dict[str, TenantLedger]:
    rows = (await db_session.execute(select(TenantLedger))).scalars().all()
    return {r.idempotency_key: r for r in rows}


def _chat_recorder(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, ma: _MA
) -> UsageRecorder:
    deps = TurnDeps(
        anthropic=ma.client,
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=MARKUP,
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        tool_safety=OPEN_TOOL_SAFETY,
    )
    return bind_recorder(
        deps,
        tenant_id=tenant_id,
        external_user_id="user-1",
        ma_session_id=SESSION,
        model_id=MODEL,
        channel_id="chan-1",
    )


def _expected_debit(n: int) -> Decimal:
    usage = ma_model_usage(input_tokens=1000 * n, output_tokens=500, cache_read_input_tokens=200)
    return -debit_amount(cost_of(usage, MODEL_PRICING[MODEL]), markup=MARKUP)


# ---------------------------------------------------------------------------
# settle_session
# ---------------------------------------------------------------------------


async def test_every_unbilled_call_is_debited_once_at_the_chat_price(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.turn("evt_a", "evt_b")
    record = _chat_recorder(db_session_factory, tenant.id, ma)

    with structlog.testing.capture_logs() as logs:
        first = await settle_session(ma.client, db_session_factory, SESSION, record=record)
    again = await settle_session(ma.client, db_session_factory, SESSION, record=record)

    ledger = await _ledger(db_session)
    assert {k: r.delta_usd for k, r in ledger.items()} == {
        f"turn:{SESSION}:evt_a": _expected_debit(1),
        f"turn:{SESSION}:evt_b": _expected_debit(2),
    }
    assert {(r.reason, r.channel_id) for r in ledger.values()} == {("turn_debit", "chan-1")}
    assert (first.debited, first.complete, len(first.unbilled)) == (2, True, 2)
    assert first.debit_usd == -sum(r.delta_usd for r in ledger.values())
    assert (again.debited, again.unbilled) == (0, ()), "a second settle debits nothing"
    assert [e["debited"] for e in logs if e["event"] == "billing.settled"] == [2]


async def test_a_call_listed_after_a_complete_settle_is_billed_by_the_next(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No stored progress: every settle reads the listing, a late call included."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.turn("evt_a", "evt_b")
    record = _chat_recorder(db_session_factory, tenant.id, ma)

    await settle_session(ma.client, db_session_factory, SESSION, record=record)
    ma.events[SESSION].append(_call("evt_late", at=NOW))  # older than evt_b, listed late
    second = await settle_session(ma.client, db_session_factory, SESSION, record=record)

    assert "created_at[gte]" not in ma.listings[-1], "no floor but the backlog cutoff"
    assert [e.id for e in second.unbilled] == ["evt_late"]
    assert len(await _ledger(db_session)) == 3


async def test_calls_the_live_stream_already_debited_are_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.turn("evt_a", "evt_b")
    live = _chat_recorder(db_session_factory, tenant.id, ma)
    await live(
        event=BetaManagedAgentsSpanModelRequestEndEvent.model_validate(ma.events[SESSION][1])
    )
    handed: list[str] = []

    async def counting(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> Decimal | None:
        handed.append(event.id)
        return await live(event=event)

    result = await settle_session(ma.client, db_session_factory, SESSION, record=counting)

    assert handed == ["evt_b"]
    assert result.debited == 1
    assert len(await _ledger(db_session)) == 2


async def test_a_page_bound_reads_the_newest_calls_and_reports_incomplete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.events[SESSION] = [_call(f"evt_{i:03d}", at=NOW + timedelta(seconds=i)) for i in range(150)]
    record = _chat_recorder(db_session_factory, tenant.id, ma)

    first = await settle_session(ma.client, db_session_factory, SESSION, record=record, max_pages=1)
    rest = await settle_session(ma.client, db_session_factory, SESSION, record=record, max_pages=2)

    assert (first.debited, first.complete) == (100, False)
    assert {e.id for e in first.unbilled} == {f"evt_{i:03d}" for i in range(50, 150)}
    assert (rest.debited, rest.complete) == (50, True), "a larger bound reads it all again"
    assert len(await _ledger(db_session)) == 150


async def test_the_deadline_bounds_a_slow_upstream(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id), stall_s=30.0)
    record = _chat_recorder(db_session_factory, tenant.id, ma)

    started = time.monotonic()
    result = await settle_session(
        ma.client,
        db_session_factory,
        SESSION,
        record=record,
        deadline=time.monotonic() + 0.2,
    )

    assert time.monotonic() - started < 2.0
    assert result.complete is False


async def test_a_read_only_settle_reports_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.turn("evt_a")

    result = await settle_session(ma.client, db_session_factory, SESSION, record=None)

    assert [e.id for e in result.unbilled] == ["evt_a"]
    assert await _ledger(db_session) == {}


# ---------------------------------------------------------------------------
# Attribution off the session's stamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin_channel", "budget_channel"),
    [(None, None), ("chan-1", "chan-1"), ("dm-1", "chan-1"), ("chan-2", None)],
    ids=["unstamped", "channel", "dm-budgeted-to-source", "seal-stamp-only"],
)
async def test_a_stamped_session_is_billed_to_its_tenant_user_and_budget_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    origin_channel: str | None,
    budget_channel: str | None,
) -> None:
    principal = await make_platform_principal(db_session, platform="discord", external_id="u-7")
    await db_session.commit()
    session = _session(
        principal.tenant_id,
        account_id=principal.account_id,
        budget_channel=budget_channel,
        origin_channel=origin_channel,
    )
    ma = _MA(session)
    ma.turn("evt_a")

    await settle_stamped_session(ma.client, db_session_factory, session, markup=MARKUP)

    usage = (await db_session.execute(select(UsageEvent))).scalars().one()
    debit = (await db_session.execute(select(TenantLedger))).scalars().one()
    assert (usage.tenant_id, usage.platform_user_id) == (principal.tenant_id, "u-7")
    assert usage.channel_id == debit.channel_id == budget_channel


async def test_sessions_that_are_not_ours_are_not_billed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    untagged = ma_session(id="sesn_untagged", metadata={})
    malformed = _session(tenant.id).model_copy(
        update={"metadata": {MA_METADATA_KEY_TENANT: "not-a-uuid"}}
    )
    foreign = _session(uuid.uuid4())

    for session in (untagged, malformed, foreign):
        assert await session_billing(db_session_factory, session) is None


@pytest.mark.parametrize("account", ["not-a-uuid", "other-tenant", "no-principal"])
async def test_a_bad_account_stamp_still_bills_the_tenant_without_a_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account: str,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="guild-a")
    other = await make_tenant(db_session, workspace_id="guild-b")
    own_account = await make_account(db_session, tenant=tenant)
    stranger = await make_platform_principal(
        db_session, platform="discord", external_id="stranger", tenant=other
    )
    await db_session.commit()
    stamp = {
        "not-a-uuid": "not-a-uuid",
        "other-tenant": str(stranger.account_id),
        "no-principal": str(own_account.id),
    }[account]

    billing = await session_billing(db_session_factory, _session(tenant.id, account_id=stamp))

    assert billing is not None
    assert (billing.tenant_id, billing.platform_user_id) == (tenant.id, None)


async def test_an_exempt_session_is_never_debited_and_its_cost_is_logged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id, exempt=True)
    ma = _MA(session)
    ma.turn("evt_a")

    with structlog.testing.capture_logs() as logs:
        await settle_stamped_session(ma.client, db_session_factory, session, markup=MARKUP)

    assert await _ledger(db_session) == {}
    (absorbed,) = [e for e in logs if e["event"] == "usage_sweep.exempt_skipped"]
    assert absorbed["model_calls"] == 1
    assert Decimal(absorbed["would_be_debit_usd"]) == -_expected_debit(1)


# ---------------------------------------------------------------------------
# Headless sends: settled once the turn has ended
# ---------------------------------------------------------------------------


async def _request(
    sessionmaker: async_sessionmaker[AsyncSession],
    turn: str | None = BOUNDARY,
    *,
    session_id: str = SESSION,
) -> datetime:
    """A settle request as a send leaves it: written first, then named (unless `turn=None`)."""
    async with sessionmaker() as s, s.begin():
        _, requested_at = await session_settle_state.request_settle(
            s, managed_session_id=session_id
        )
        if turn is not None:
            await session_settle_state.name_settle_turn(
                s, managed_session_id=session_id, requested_at=requested_at, turn_event_id=turn
            )
    return requested_at


async def _pending_request(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> session_settle_state.PendingSettle | None:
    async with sessionmaker() as s:
        return await session_settle_state.settle_request(s, managed_session_id=SESSION)


async def _pending(sessionmaker: async_sessionmaker[AsyncSession]) -> str | None:
    request = await _pending_request(sessionmaker)
    return None if request is None else request.turn_event_id or "unnamed"


async def test_a_stale_idle_before_the_turn_ends_does_not_settle(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Right after a send the session can still read idle from the previous turn."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.events[SESSION] = [
        _idle("evt_prev_idle", at=NOW - timedelta(minutes=1)),
        _message(BOUNDARY, at=NOW),
        _call("evt_running", at=NOW + timedelta(seconds=1)),
    ]
    await _request(db_session_factory)

    request = await _pending_request(db_session_factory)
    assert request is not None and await turn_ended(ma.client, SESSION, request) is False
    assert await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP) is None
    assert await _pending(db_session_factory) == BOUNDARY, "the request waits for the turn"
    assert await _ledger(db_session) == {}


async def test_a_finished_turn_is_settled_once_and_its_request_cleared(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.turn("evt_a", "evt_b")
    await _request(db_session_factory)

    first = await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP)
    listings = len(ma.listings)
    again = await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP)

    assert first is not None and first.debited == 2
    assert again is None and len(ma.listings) == listings, "a repeated poll reads nothing"
    assert await _pending(db_session_factory) is None
    assert len(await _ledger(db_session)) == 2


async def test_a_settle_cut_short_keeps_the_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.events[SESSION] = [
        _message(BOUNDARY, at=NOW),
        *[_call(f"evt_{i:03d}", at=NOW + timedelta(seconds=i + 1)) for i in range(150)],
        _idle("evt_idle", at=NOW + timedelta(minutes=5)),
    ]
    await _request(db_session_factory)

    cut = await settle_finished_turn(
        ma.client, db_session_factory, session, markup=MARKUP, max_pages=1
    )
    kept = await _pending(db_session_factory)
    rest = await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP)

    assert cut is not None and not cut.complete and kept == BOUNDARY
    assert rest is not None and rest.complete and await _pending(db_session_factory) is None
    assert len(await _ledger(db_session)) == 150


async def test_a_newer_send_keeps_its_own_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _request(db_session_factory)
    first = await _pending_request(db_session_factory)
    await _request(db_session_factory, "evt_turn_b")
    assert first is not None
    async with db_session_factory() as s, s.begin():
        await session_settle_state.clear_settle_request(
            s, managed_session_id=SESSION, request=first
        )
    assert await _pending(db_session_factory) == "evt_turn_b"


async def test_an_upstream_error_never_fails_the_read_and_keeps_the_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    await _request(db_session_factory)
    broken = build_fake_anthropic(lambda _r: httpx.Response(500, json={"type": "error"}))

    assert await settle_finished_turn(broken, db_session_factory, session, markup=MARKUP) is None
    assert await _pending(db_session_factory) == BOUNDARY


async def test_the_drain_settles_finished_sends_and_leaves_running_ones(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    done = _session(tenant.id)
    running = _session(tenant.id, session_id="sesn_running", status="running")
    ma = _MA(done, running)
    ma.turn("evt_a")
    ma.turn("evt_r", session_id="sesn_running")
    for sid in (SESSION, "sesn_running", "sesn_gone"):
        await _request(db_session_factory, session_id=sid)

    debited = await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)

    async with db_session_factory() as s:
        pending = await session_settle_state.list_settle_requests(s, limit=10)
    assert debited == 1
    assert pending == ["sesn_running"], "running kept; settled and gone cleared"
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}


# ---------------------------------------------------------------------------
# The backlog: calls from before this path was deployed
# ---------------------------------------------------------------------------


async def _cutoff(sessionmaker: async_sessionmaker[AsyncSession], at: datetime) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO usage_reconcile_progress (managed_session_id, covered_through) "
                "VALUES (:id, :at)"
            ),
            {"id": session_settle_state.BACKLOG_CUTOFF_ID, "at": at},
        )


async def test_automatic_settles_never_read_before_the_backlog_cutoff(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.events[SESSION] = [_call("evt_backlog", at=NOW - timedelta(days=1))]
    ma.turn("evt_new")
    await _cutoff(db_session_factory, NOW - timedelta(hours=1))

    result = await settle_stamped_session(ma.client, db_session_factory, session, markup=MARKUP)

    assert result is not None and [e.id for e in result.unbilled] == ["evt_new"]
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_new"}
    assert datetime.fromisoformat(ma.listings[-1]["created_at[gte]"]) == NOW - timedelta(hours=1)


async def test_the_backlog_is_debited_for_internal_tenants_and_waived_for_the_rest(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    internal = await make_tenant(db_session, workspace_id="internal")
    customer = await make_tenant(db_session, workspace_id="customer")
    await db_session.commit()
    ours = _session(internal.id)
    theirs = _session(customer.id, session_id="sesn_customer")
    exempt = _session(internal.id, session_id="sesn_exempt", exempt=True)
    ma = _MA(ours, theirs, exempt)
    before = NOW - timedelta(days=1)
    ma.turn("evt_a", "evt_b", at=before)
    ma.turn("evt_c", session_id="sesn_customer", at=before)
    ma.turn("evt_d", session_id="sesn_exempt", at=before)
    ma.events["sesn_customer"].append(_call("evt_after", at=NOW))
    await _cutoff(db_session_factory, NOW - timedelta(hours=1))
    async with db_session_factory() as s, s.begin():
        for sid, tenant_id in (
            (SESSION, internal.id),
            ("sesn_customer", customer.id),
            ("sesn_exempt", internal.id),
        ):
            await turn_outcomes.record(s, _outcome(sid, tenant_id))
    allow = frozenset({internal.id})

    with structlog.testing.capture_logs() as dry_logs:
        dry = await settle_backlog(ma.client, db_session_factory, internal=allow, markup=MARKUP)
    assert await _ledger(db_session) == {}, "a dry run writes nothing"
    with structlog.testing.capture_logs() as logs:
        applied = await settle_backlog(
            ma.client, db_session_factory, internal=allow, markup=MARKUP, apply=True
        )

    summary = {(t.tenant_id, t.kind, t.sessions, t.calls) for t in dry.tenants}
    assert summary == {(internal.id, "internal", 1, 2), (customer.id, "external", 1, 1)}
    assert {(t.tenant_id, t.kind, t.sessions, t.calls) for t in applied.tenants} == summary
    assert (dry.mode, applied.mode) == ("dry_run", "apply")
    assert dry.cutoff == applied.cutoff == NOW - timedelta(hours=1)
    internal_dry, external_dry = (
        next(t for t in dry.tenants if t.kind == k) for k in ("internal", "external")
    )
    internal_applied, external_applied = (
        next(t for t in applied.tenants if t.kind == k) for k in ("internal", "external")
    )
    internal_usd = -(_expected_debit(1) + _expected_debit(2))
    assert (internal_dry.would_debit_usd, internal_dry.debited_usd) == (internal_usd, None)
    assert (
        internal_applied.would_debit_usd,
        internal_applied.debited_usd,
        internal_applied.waived_usd,
    ) == (
        internal_usd,
        internal_usd,
        None,
    ), "applied: what the ledger was actually debited"
    assert external_dry.waived_usd == external_applied.waived_usd == -_expected_debit(1)
    assert external_applied.debited_usd is None
    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a", f"turn:{SESSION}:evt_b"}
    assert not [e for e in dry_logs if e["event"] == "billing.backlog_waived"]
    (waived,) = [e for e in logs if e["event"] == "billing.backlog_waived"]
    assert waived["tenant_id"] == str(customer.id)
    # The customer's post-cutoff call is ordinary billing, settled as usual.
    later = await settle_stamped_session(ma.client, db_session_factory, theirs, markup=MARKUP)
    assert later is not None and [e.id for e in later.unbilled] == ["evt_after"]


def _outcome(session_id: str, tenant_id: uuid.UUID) -> turn_outcomes.OutcomeRecord:
    return turn_outcomes.OutcomeRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        account_id=None,
        platform="mcp",
        channel_id=None,
        thread_id=None,
        agent_id=None,
        session_id=session_id,
        origin="chat",
        reason=TerminationReason.UNKNOWN,
        started_at=NOW,
        ended_at=NOW,
        duration_ms=0,
        recovered=False,
        error_class=None,
        release="test",
        usage_refs=[],
    )


# ---------------------------------------------------------------------------
# Review regressions: backlog scope, turn-end proof, durable progress, read
# budget, drain isolation
# ---------------------------------------------------------------------------


async def test_backlog_apply_debits_exactly_the_calls_the_dry_run_reported(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ma = _MA(_session(tenant.id))
    ma.events[SESSION] = [_call("old", at=NOW - timedelta(days=1)), _call("new", at=NOW)]
    await _cutoff(db_session_factory, NOW - timedelta(hours=1))
    args: dict[str, Any] = {
        "internal": frozenset({tenant.id}),
        "markup": MARKUP,
        "session_ids": [SESSION],
    }

    dry = await settle_backlog(ma.client, db_session_factory, **args)
    applied = await settle_backlog(ma.client, db_session_factory, apply=True, **args)

    ledger = await _ledger(db_session)
    assert set(ledger) == {f"turn:{SESSION}:old"}, "the post-cutoff call is ordinary billing"
    (dry_total,), (applied_total,) = dry.tenants, applied.tenants
    old_usd = -ledger[f"turn:{SESSION}:old"].delta_usd
    assert (dry_total.calls, dry_total.would_debit_usd) == (1, old_usd)
    assert (applied_total.calls, applied_total.debited_usd) == (1, old_usd)


@pytest.mark.parametrize("previous_idle", [False, True], ids=["nothing-listed", "previous-idle"])
async def test_a_turn_not_listed_yet_cannot_be_taken_as_ended(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    previous_idle: bool,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    if previous_idle:
        ma.events[SESSION] = [_idle("previous_idle", at=NOW - timedelta(minutes=1))]
    await _request(db_session_factory)

    await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP)
    assert await _pending(db_session_factory) == BOUNDARY, "the request waits for its message"
    ma.turn("later_call")
    await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)

    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:later_call"}


async def test_an_unnamed_request_waits_on_the_first_message_after_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The write naming the accepted send failed: the request still finds its turn."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.events[SESSION] = [
        _message("evt_previous", at=NOW - timedelta(days=1)),
        _idle("previous_idle", at=NOW - timedelta(days=1)),
    ]
    await _request(db_session_factory, turn=None)
    request = await _pending_request(db_session_factory)
    assert request is not None and request.turn_event_id is None

    assert await turn_ended(ma.client, SESSION, request) is False
    after = request.requested_at + timedelta(seconds=1)
    ma.events[SESSION] += [_message("evt_new", at=after), _call("evt_c", at=after)]
    assert await turn_ended(ma.client, SESSION, request) is False
    ma.events[SESSION].append(_idle("evt_idle", at=after + timedelta(seconds=1)))
    assert await turn_ended(ma.client, SESSION, request) is True


@pytest.mark.parametrize("shape", ["equal-timestamps", "late-call"])
async def test_the_drain_completes_what_a_bounded_read_could_not(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    shape: str,
) -> None:
    """A session too big for a read's ten pages keeps its request, and the drain's
    larger bound settles it whole: calls sharing one timestamp, or one listed late
    behind newer calls, included."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    calls = [
        _call(
            f"call_{i:04d}",
            at=NOW + (timedelta(0) if shape == "equal-timestamps" else timedelta(seconds=i)),
        )
        for i in range(1001)
    ]
    ma.events[SESSION] = [_message(BOUNDARY, at=NOW - timedelta(seconds=1)), *calls]
    ma.events[SESSION].append(_idle("evt_idle", at=NOW + timedelta(hours=1)))
    await _request(db_session_factory)

    # The read stops at its ten pages (or its budget, on a slow runner); either way
    # it keeps the request, and only the billed state after the drain matters.
    read = await settle_finished_turn(
        ma.client, db_session_factory, session, markup=MARKUP, budget_s=None
    )
    assert read is None or not read.complete
    assert await _pending(db_session_factory) == BOUNDARY
    if shape == "late-call":
        ma.events[SESSION].append(_call("late", at=NOW + timedelta(seconds=997)))
    await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)

    assert len(await _ledger(db_session)) == 1001 + (shape == "late-call")
    assert await _pending(db_session_factory) is None


async def test_a_young_unnamed_request_is_left_to_its_send(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A read while the next send is in flight never consumes that send's request."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.turn("evt_a")  # the previous turn, finished
    await _request(db_session_factory, turn=None)  # the next send's request, not named yet

    assert await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP) is None
    assert await _pending(db_session_factory) == "unnamed"
    assert ma.listings == [], "nothing is read for it"


async def test_a_settle_never_clears_a_request_written_after_it_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.turn("evt_a")
    await _request(db_session_factory)
    real = usage_sweep.settle_stamped_session

    async def next_send_meanwhile(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        await _request(db_session_factory, "evt_turn_b")
        return result

    monkeypatch.setattr(usage_sweep, "settle_stamped_session", next_send_meanwhile)
    await settle_finished_turn(ma.client, db_session_factory, session, markup=MARKUP)

    assert await _pending(db_session_factory) == "evt_turn_b"


@pytest.mark.parametrize("stage", ["turn-check", "settle"])
async def test_any_rate_limit_ends_the_drain_pass(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    stage: str,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    first = _session(tenant.id, session_id="sesn_first")
    second = _session(tenant.id)
    ma = _MA(first, second)
    ma.turn("evt_1", session_id="sesn_first")
    ma.turn("evt_2")
    await _request(db_session_factory, session_id="sesn_first")
    await _request(db_session_factory)
    real = ma._list  # pyright: ignore[reportPrivateUsage]
    limited_type = "user.message" if stage == "turn-check" else "span.model_request_end"

    async def limited(request: httpx.Request, match: Any) -> httpx.Response:
        if "sesn_first" in request.url.path and limited_type in request.url.params.get_list(
            "types[]"
        ):
            return httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            )
        return await real(request, match)

    ma._list = limited  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    router = MARouter()
    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events", cast(Any, limited))
    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)", cast(Any, ma._retrieve))  # pyright: ignore[reportPrivateUsage]
    client = build_fake_anthropic(router.dispatch).with_options(max_retries=0)

    with pytest.raises(anthropic.RateLimitError):
        await sweep_headless_usage(client, db_session_factory, markup=MARKUP)

    assert await _ledger(db_session) == {}, "the pass stopped before the next session"


async def test_the_read_budget_covers_the_database_work(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.turn("evt_a")
    await _request(db_session_factory)
    real = usage_sweep.session_billing

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.25)
        return await real(*args, **kwargs)

    monkeypatch.setattr(usage_sweep, "session_billing", slow)
    started = time.monotonic()
    result = await settle_finished_turn(
        ma.client, db_session_factory, session, markup=MARKUP, budget_s=0.05
    )

    assert time.monotonic() - started < 0.2
    assert result is None and await _pending(db_session_factory) == BOUNDARY


async def test_a_session_the_drain_cannot_read_never_blocks_the_others(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    session = _session(tenant.id)
    ma = _MA(session)
    ma.turn("good_call")
    await _request(db_session_factory, "bad", session_id="sesn_poison")
    await _request(db_session_factory)
    real = ma.client.beta.sessions.retrieve

    async def retrieve(session_id: str, **kwargs: Any) -> Any:
        if session_id == "sesn_poison":
            response = httpx.Response(403, request=httpx.Request("GET", "https://x/v1/sessions"))
            raise anthropic.PermissionDeniedError("forbidden", response=response, body={})
        return await real(session_id, **kwargs)

    monkeypatch.setattr(ma.client.beta.sessions, "retrieve", retrieve)
    await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)

    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:good_call"}
    async with db_session_factory() as s:
        assert await session_settle_state.list_settle_requests(s, limit=10) == ["sesn_poison"]


async def test_the_drain_visits_waiting_sessions_in_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests still running go to the back of the queue, so later ones get visited."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    monkeypatch.setattr(usage_sweep, "_DRAIN_BATCH", 1)
    running = _session(tenant.id, session_id="sesn_running", status="running")
    done = _session(tenant.id)
    ma = _MA(running, done)
    ma.turn("evt_a")
    await _request(db_session_factory, session_id="sesn_running")
    await _request(db_session_factory)

    await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)
    await sweep_headless_usage(ma.client, db_session_factory, markup=MARKUP)

    assert set(await _ledger(db_session)) == {f"turn:{SESSION}:evt_a"}

"""Tests for daimon.core.usage_sweep — headless MCP turn metering backfill.

Headless agent-chat turns (start_turn over MCP) create an MA session and send a
user.message but never drive the SSE stream, so the live `record_turn_usage`
hook never fires and no usage_events/tenant_ledger rows land. This sweep reads
each MA session's `span.model_request_end` events out of band and replays them
through `record_turn_usage`, which is idempotent on (managed_session_id,
event_id) — so repeated sweeps never double-count.

Attribution comes off the session metadata that `create_session` stamps:
`daimon_tenant` (the billed tenant) and `daimon_account` (resolved to the
owning human's platform_user_id for per-member reporting).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core._models import Tenant, TenantLedger, UsageEvent, UsageSweepSession
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.stores import usage_sweep_sessions
from daimon.core.usage_sweep import UsageSweepWatermark, sweep_headless_usage
from daimon.testing.factories import make_account, make_platform_principal
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
)
from daimon.testing.ma_models import ma_model_usage, ma_session, ma_session_agent
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def no_real_sweep_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daimon.core.usage_sweep._REQUEST_INTERVAL_S", 0.0)


NOW = datetime.now(UTC)


async def _add_owned_sessions(
    router: MARouter, db_session: AsyncSession, sessions: list[dict[str, Any]]
) -> None:
    """Register test-owned IDs and serve retrieval; workspace listing is absent."""
    known = set((await db_session.scalars(select(Tenant.id))).all())
    for shape in sessions:
        raw = shape.get("metadata", {}).get(MA_METADATA_KEY_TENANT)
        try:
            tenant_id = uuid.UUID(raw) if raw else None
        except ValueError:
            tenant_id = next(iter(known), None)
        if tenant_id in known:
            await usage_sweep_sessions.register(
                db_session, session_id=shape["id"], tenant_id=tenant_id
            )
        await db_session.execute(
            update(UsageSweepSession)
            .where(UsageSweepSession.session_id == shape["id"])
            .values(updated_at=NOW)
        )
        router.add(
            "GET",
            rf"/v1/sessions/{shape['id']}",
            lambda req, m, body=shape: httpx.Response(200, json=body),
        )
    await db_session.flush()


def _session_dict(
    *,
    session_id: str,
    tenant_id: uuid.UUID | str,
    account_id: uuid.UUID | str,
    model: str = "claude-sonnet-4-6",
    billing_exempt: str | None = None,
    origin_channel_id: str | None = None,
    budget_channel_id: str | None = None,
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    """A headless MA session tagged the way create_session tags it."""
    metadata = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_ACCOUNT: str(account_id),
    }
    if billing_exempt is not None:
        metadata[MA_METADATA_KEY_BILLING_EXEMPT] = billing_exempt
    if origin_channel_id is not None:
        metadata[MA_METADATA_KEY_CHANNEL] = origin_channel_id
    if budget_channel_id is not None:
        metadata[MA_METADATA_KEY_BUDGET_CHANNEL] = budget_channel_id
    s = ma_session(
        id=session_id,
        agent=ma_session_agent(id="agent_headless1", name="headless-agent", model=model),
        environment_id="env_headless1",
        metadata=metadata,
        created_at=NOW,
        updated_at=updated_at,
    )
    payload = s.model_dump(mode="json")
    payload["usage"] = {"input_tokens": 0, "output_tokens": 0}
    return payload


async def test_sweep_reads_recent_or_unsettled_only_and_keeps_progress_on_restart(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="watermark-user"
    )
    sessions = [
        _session_dict(
            session_id=sid,
            tenant_id=principal.tenant_id,
            account_id=principal.account_id,
            updated_at=NOW,
        )
        for sid in ("sesn_old_settled", "sesn_old_unsettled", "sesn_recent")
    ]
    router = MARouter()
    await _add_owned_sessions(router, db_session, sessions)
    await db_session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id.like("sesn_old_%"))
        .values(updated_at=NOW - timedelta(days=1))
    )
    await db_session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id == "sesn_old_settled")
        .values(unsettled=False, last_swept_at=NOW - timedelta(days=1), remote_status="idle")
    )
    reads: list[str] = []

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        reads.append(req.url.path)
        return list_response([])

    router.add("GET", r"/v1/sessions/[^/]+/events", events)
    client = build_fake_anthropic(router.dispatch)
    for minute in (0, 1, 60, 180):
        reads.clear()
        await sweep_headless_usage(
            client,
            db_session_factory,
            markup=Decimal("1"),
            watermark=UsageSweepWatermark(),
            now=NOW + timedelta(minutes=minute),
        )
        if minute == 0:
            assert set(reads) == {
                "/v1/sessions/sesn_recent/events",
                "/v1/sessions/sesn_old_unsettled/events",
            }
        elif minute == 60:
            assert reads == ["/v1/sessions/sesn_recent/events"]
        else:
            assert reads == [], "restart never rescans old settled histories"


def _model_request_end_dict(
    *, event_id: str, input_tokens: int, output_tokens: int
) -> dict[str, Any]:
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=input_tokens, output_tokens=output_tokens),
        processed_at=NOW,
        type="span.model_request_end",
    )
    return event.model_dump(mode="json")


async def test_sweep_records_usage_for_headless_session_attributed_to_tenant_and_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A start_turn-driven session's model_request_end event lands in usage_events
    attributed to its daimon_tenant and the owning account's platform_user_id."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-42"
    )

    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_headless",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_1", input_tokens=100, output_tokens=50)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(rows) == 1, "sweep should record exactly one usage row for the session's event"
    row = rows[0]
    assert row.tenant_id == principal.tenant_id, "row must be attributed to the session's tenant"
    assert row.platform_user_id == "discord-user-42", (
        "platform_user_id must resolve from the session's daimon_account"
    )
    assert row.managed_session_id == "sesn_headless", "managed_session_id is the swept session id"
    assert row.event_id == "evt_1", "event_id is the span.model_request_end event id"
    assert row.input_tokens == 100, "tokens sourced from event.model_usage"
    assert row.output_tokens == 50, "tokens sourced from event.model_usage"


@pytest.mark.parametrize(
    ("origin_channel_id", "budget_channel_id"),
    [(None, None), ("chan-1", "chan-1"), ("dm-1", "chan-1"), ("chan-2", None)],
    ids=["unstamped", "channel", "dm-budgeted-to-source", "seal-stamp-only"],
)
async def test_sweep_attributes_spend_to_the_budget_channel_stamp(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    origin_channel_id: str | None,
    budget_channel_id: str | None,
) -> None:
    """`daimon_budget_channel` lands on both rows, so the channel's budget counts the
    debit; `daimon_channel` (where the conversation runs, for the seal) never does."""
    principal = await make_platform_principal(db_session, platform="discord", external_id="u-7")
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_channel",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
                origin_channel_id=origin_channel_id,
                budget_channel_id=budget_channel_id,
            )
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_1", input_tokens=100, output_tokens=50)]
        ),
    )

    await sweep_headless_usage(
        build_fake_anthropic(router.dispatch), db_session_factory, markup=Decimal("1.0")
    )

    usage = (await db_session.execute(select(UsageEvent))).scalars().one()
    debit = (await db_session.execute(select(TenantLedger))).scalars().one()
    assert usage.channel_id == budget_channel_id, "usage row should carry the budget channel"
    assert debit.channel_id == budget_channel_id, "debit row should carry the budget channel"


async def test_sweep_idempotent_across_runs_no_double_count(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running the sweep twice over the same session records the event once."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-99"
    )

    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_replay",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_replay", input_tokens=10, output_tokens=5)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))
    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.managed_session_id == "sesn_replay")
        )
    ).scalar_one()
    assert count == 1, "replaying the sweep must not double-count the same event"


async def test_sweep_requests_only_model_calls_and_replays_only_unrecorded_ones(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pass over a metered session asks the API for model calls only and
    writes nothing for calls already in usage_events when activity wakes it."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-skip"
    )
    events = [_model_request_end_dict(event_id="evt_old", input_tokens=10, output_tokens=5)]
    requested_types: list[list[str]] = []

    def serve_events(req: httpx.Request, match: Any) -> httpx.Response:
        requested_types.append(req.url.params.get_list("types[]"))
        return list_response(events)

    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_skip",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    router.add("GET", r"/v1/sessions/[^/]+/events", serve_events)
    client = build_fake_anthropic(router.dispatch)

    first = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))
    idle = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))
    events.append(_model_request_end_dict(event_id="evt_new", input_tokens=20, output_tokens=5))
    await usage_sweep_sessions.register(
        db_session, session_id="sesn_skip", tenant_id=principal.tenant_id
    )
    later = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    assert requested_types == [["span.model_request_end"]] * 3, (
        f"every event read is filtered to model calls server-side, got {requested_types}"
    )
    assert (first, idle, later) == (1, 0, 1), (
        f"only calls not yet recorded are replayed, got {(first, idle, later)}"
    )
    recorded = (
        (
            await db_session.execute(
                select(UsageEvent.event_id).where(UsageEvent.managed_session_id == "sesn_skip")
            )
        )
        .scalars()
        .all()
    )
    assert sorted(recorded) == ["evt_new", "evt_old"], "both calls are metered exactly once"
    debits = (
        await db_session.execute(
            select(func.count())
            .select_from(TenantLedger)
            .where(TenantLedger.idempotency_key.like("turn:sesn_skip:%"))
        )
    ).scalar_one()
    assert debits == 2, "each call is debited once"


async def test_sweep_skips_session_without_tenant_tag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An untagged session (no daimon_tenant — e.g. a DM or foreign session) is
    skipped: no usage row, and its events are never even fetched."""
    s = ma_session(
        id="sesn_untagged",
        agent=ma_session_agent(id="agent_x", name="x"),
        environment_id="env_x",
        created_at=NOW,
    )
    router = MARouter()
    await _add_owned_sessions(router, db_session, [s.model_dump(mode="json")])

    def _events_must_not_be_called(req: httpx.Request, m: Any) -> httpx.Response:
        raise AssertionError("events must not be fetched for an untagged session")

    router.add("GET", r"/v1/sessions/[^/]+/events", _events_must_not_be_called)
    client = build_fake_anthropic(router.dispatch)

    recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    assert recorded == 0, "untagged session contributes no recorded events"
    count = (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    assert count == 0, "no usage row for an untagged session"


async def test_sweep_skips_session_whose_tenant_is_not_in_db(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session tagged with a daimon_tenant that has no Tenant row in this DB is
    skipped — no usage row, events never fetched. A shared MA workspace holds
    sessions from other deployments/evals; recording them would trip the
    usage_events tenant_id FK and crash the scheduler tick."""
    foreign_tenant = uuid.uuid4()  # never inserted into tenants
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_foreign",
                tenant_id=foreign_tenant,
                account_id=uuid.uuid4(),
            )
        ],
    )

    def _events_must_not_be_called(req: httpx.Request, m: Any) -> httpx.Response:
        raise AssertionError("events must not be fetched for a foreign-tenant session")

    router.add("GET", r"/v1/sessions/[^/]+/events", _events_must_not_be_called)
    client = build_fake_anthropic(router.dispatch)

    recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    assert recorded == 0, "foreign-tenant session contributes no recorded events"
    count = (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    assert count == 0, "no usage row for a tenant this deployment does not own"


async def test_sweep_records_with_null_platform_user_when_account_has_no_discord_principal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An account with no discord principal still bills the tenant: the usage row
    is written with platform_user_id=None (the ledger debit keys on tenant_id)."""
    account = await make_account(db_session)  # account + tenant, no discord principal

    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_no_principal",
                tenant_id=account.tenant_id,
                account_id=account.id,
            )
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_np", input_tokens=7, output_tokens=3)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(rows) == 1, "usage is recorded even without a resolvable platform user"
    assert rows[0].platform_user_id is None, "platform_user_id is None when no discord principal"
    assert rows[0].tenant_id == account.tenant_id, "tenant attribution still correct"


async def test_sweep_skips_malformed_tenant_and_continues_to_later_valid_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A malformed tenant tag cannot abort billing for later valid sessions."""
    account = await make_account(db_session)
    sessions = [
        _session_dict(
            session_id="sesn_bad_tenant",
            tenant_id="not-a-uuid",
            account_id=account.id,
        ),
        _session_dict(
            session_id="sesn_after_bad_tenant",
            tenant_id=account.tenant_id,
            account_id=account.id,
        ),
    ]
    router = MARouter()
    await _add_owned_sessions(router, db_session, sessions)
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [
                _model_request_end_dict(
                    event_id="evt_after_bad_tenant", input_tokens=7, output_tokens=3
                )
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    debits = (await db_session.execute(select(TenantLedger))).scalars().all()
    assert recorded == 1, "sweep should record the valid event after malformed tenant metadata"
    assert [(row.managed_session_id, row.tenant_id) for row in rows] == [
        ("sesn_after_bad_tenant", account.tenant_id)
    ], "malformed tenant metadata must be skipped without guessing a billing tenant"
    assert len(debits) == 1, "the valid later event should produce exactly one debit"
    assert debits[0].tenant_id == account.tenant_id, "debit must stay on the valid session tenant"
    assert debits[0].delta_usd == Decimal("-0.000066"), (
        "valid later event debit must match its 7 input and 3 output tokens"
    )
    matching = [entry for entry in logs if entry.get("event") == "usage_sweep.session_skipped"]
    assert len(matching) == 1, "malformed tenant metadata should emit one structured warning"
    assert matching[0]["log_level"] == "warning", "malformed tenant warning should be a warning"
    assert matching[0]["session_id"] == "sesn_bad_tenant", (
        "malformed tenant warning should identify the session"
    )
    assert matching[0]["reason"] == "invalid_tenant_metadata", (
        "malformed tenant warning should provide a stable reason"
    )
    assert "not-a-uuid" not in repr(matching[0]), "warning must not include raw malformed metadata"


async def test_sweep_bills_valid_tenant_with_malformed_account_and_retry_is_idempotent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bad optional account metadata drops member attribution, not tenant billing."""
    account = await make_account(db_session)
    principal = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="discord-user-after-bad-account",
        account=account,
    )
    sessions = [
        _session_dict(
            session_id="sesn_bad_account",
            tenant_id=account.tenant_id,
            account_id="not-a-uuid",
        ),
        _session_dict(
            session_id="sesn_after_bad_account",
            tenant_id=account.tenant_id,
            account_id=account.id,
        ),
    ]
    events = {
        "sesn_bad_account": _model_request_end_dict(
            event_id="evt_bad_account", input_tokens=7, output_tokens=3
        ),
        "sesn_after_bad_account": _model_request_end_dict(
            event_id="evt_after_bad_account", input_tokens=11, output_tokens=4
        ),
    }
    router = MARouter()
    await _add_owned_sessions(router, db_session, sessions)
    router.add(
        "GET",
        r"/v1/sessions/(?P<session_id>[^/]+)/events",
        lambda req, m: list_response([events[m.group("session_id")]]),
    )
    client = build_fake_anthropic(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        first_recorded = await sweep_headless_usage(
            client, db_session_factory, markup=Decimal("1.0")
        )
        retry_recorded = await sweep_headless_usage(
            client, db_session_factory, markup=Decimal("1.0")
        )

    rows = (
        (await db_session.execute(select(UsageEvent).order_by(UsageEvent.managed_session_id)))
        .scalars()
        .all()
    )
    debits = (
        (
            await db_session.execute(
                select(TenantLedger)
                .where(TenantLedger.reason == "turn_debit")
                .order_by(TenantLedger.idempotency_key)
            )
        )
        .scalars()
        .all()
    )
    assert first_recorded == 2, (
        "valid-tenant sessions should both be billed despite bad account metadata"
    )
    assert retry_recorded == 0, "retry skips both events, already recorded"
    assert [(row.managed_session_id, row.tenant_id, row.platform_user_id) for row in rows] == [
        ("sesn_after_bad_account", account.tenant_id, principal.external_id),
        ("sesn_bad_account", account.tenant_id, None),
    ], "both usage rows use the valid tenant and omit unavailable member attribution"
    assert len(debits) == 2, "each distinct model event should have exactly one debit after retry"
    assert {debit.tenant_id for debit in debits} == {account.tenant_id}, (
        "every debit must be attributed to the session's valid tenant"
    )
    assert {debit.idempotency_key for debit in debits} == {
        "turn:sesn_bad_account:evt_bad_account",
        "turn:sesn_after_bad_account:evt_after_bad_account",
    }, "debits must retain one event-specific idempotency key each"
    assert {debit.idempotency_key: debit.delta_usd for debit in debits} == {
        "turn:sesn_bad_account:evt_bad_account": Decimal("-0.000066"),
        "turn:sesn_after_bad_account:evt_after_bad_account": Decimal("-0.000093"),
    }, "each tenant debit must exactly match the corresponding model usage cost"
    account_warnings = [
        entry for entry in logs if entry.get("event") == "usage_sweep.member_attribution_omitted"
    ]
    assert len(account_warnings) >= 1, (
        "each selected malformed account warns; idle sessions may wait for a later pass"
    )
    assert all(entry["reason"] == "invalid_account_metadata" for entry in account_warnings), (
        "malformed account warnings should use a stable reason"
    )
    assert all(entry["session_id"] == "sesn_bad_account" for entry in account_warnings), (
        "malformed account warnings should identify the affected session"
    )
    assert all("not-a-uuid" not in repr(entry) for entry in account_warnings), (
        "warnings must not include raw malformed metadata"
    )


async def test_sweep_omits_platform_user_from_account_owned_by_another_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A valid foreign account UUID must not cross tenant attribution boundaries."""
    account_a = await make_account(db_session)
    account_b = await make_account(db_session)
    principal_b = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="discord-user-tenant-b",
        account=account_b,
    )
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_tenant_a_foreign_account",
                tenant_id=account_a.tenant_id,
                account_id=account_b.id,
            )
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_tenant_a", input_tokens=7, output_tokens=3)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    debits = (await db_session.execute(select(TenantLedger))).scalars().all()
    assert recorded == 1, "foreign account attribution must not suppress valid tenant billing"
    assert len(rows) == 1, "the valid tenant session should produce one usage row"
    assert rows[0].tenant_id == account_a.tenant_id, (
        "usage must remain attributed to session tenant A"
    )
    assert rows[0].platform_user_id is None, (
        "a principal owned by tenant B must not be attributed to tenant A's session"
    )
    assert len(debits) == 1, "the session should create exactly one matching debit"
    assert debits[0].tenant_id == account_a.tenant_id, "the debit must remain on session tenant A"
    assert debits[0].tenant_id != account_b.tenant_id, (
        "the foreign account tenant must not be debited"
    )
    assert debits[0].delta_usd == Decimal("-0.000066"), (
        "the debit must match the session's 7 input and 3 output tokens"
    )
    assert principal_b.external_id == "discord-user-tenant-b", (
        "the test's foreign account resolves to a real tenant B principal"
    )
    warnings = [
        entry for entry in logs if entry.get("event") == "usage_sweep.member_attribution_omitted"
    ]
    assert len(warnings) == 1, "cross-tenant account metadata should emit one structured warning"
    assert warnings[0]["log_level"] == "warning", "cross-tenant attribution should warn"
    assert warnings[0]["session_id"] == "sesn_tenant_a_foreign_account", (
        "cross-tenant warning should identify the session"
    )
    assert warnings[0]["reason"] == "account_tenant_mismatch", (
        "cross-tenant warning should give a stable reason"
    )
    assert account_b.id.hex not in repr(warnings[0]), (
        "cross-tenant warning must not expose raw account metadata"
    )


async def _two_session_router(
    db_session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> MARouter:
    """One exempt session and one billed session of the same tenant.

    The exempt one has two model calls priced at claude-sonnet-4-6
    ($3/M input, $15/M output): 1M in + 100k out ($4.50) and 200k in + 20k out
    ($0.90), so its would-be cost is exactly $5.40.
    """
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_exempt",
                tenant_id=tenant_id,
                account_id=account_id,
                billing_exempt="mcp-internal-caller",
            ),
            _session_dict(
                session_id="sesn_billed",
                tenant_id=tenant_id,
                account_id=account_id,
            ),
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/sesn_exempt/events",
        lambda req, m: list_response(
            [
                _model_request_end_dict(
                    event_id="evt_ex1", input_tokens=1_000_000, output_tokens=100_000
                ),
                _model_request_end_dict(
                    event_id="evt_ex2", input_tokens=200_000, output_tokens=20_000
                ),
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/sesn_billed/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_b1", input_tokens=10, output_tokens=5)]
        ),
    )
    return router


async def test_sweep_does_not_debit_billing_exempt_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session stamped daimon_billing_exempt (created for a BillingExempt
    caller) is not replayed: no usage row and no tenant_ledger debit. The
    operator absorbs that usage (docs/billing.md)."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-exempt"
    )
    client = build_fake_anthropic(
        (
            await _two_session_router(
                db_session, tenant_id=principal.tenant_id, account_id=principal.account_id
            )
        ).dispatch
    )

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    exempt_usage = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.managed_session_id == "sesn_exempt")
        )
    ).scalar_one()
    exempt_debits = (
        await db_session.execute(
            select(func.count())
            .select_from(TenantLedger)
            .where(TenantLedger.idempotency_key.like("turn:sesn_exempt:%"))
        )
    ).scalar_one()
    assert exempt_debits == 0, (
        f"a BillingExempt session must not be debited to the tenant, got {exempt_debits} debits"
    )
    assert exempt_usage == 0, f"a BillingExempt session must not get usage rows, got {exempt_usage}"


async def test_sweep_still_debits_billed_session_next_to_exempt_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The backstop is unchanged for billed sessions: skipping an exempt
    session does not skip the tenant's other sessions."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-billed"
    )
    client = build_fake_anthropic(
        (
            await _two_session_router(
                db_session, tenant_id=principal.tenant_id, account_id=principal.account_id
            )
        ).dispatch
    )

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    keys = (
        (
            await db_session.execute(
                select(TenantLedger.idempotency_key).where(
                    TenantLedger.idempotency_key.like("turn:%")
                )
            )
        )
        .scalars()
        .all()
    )
    assert keys == ["turn:sesn_billed:evt_b1"], (
        f"only the billed session's call is debited, got {keys}"
    )


async def test_sweep_logs_absorbed_cost_of_exempt_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The skipped session's would-be spend is logged per session
    (usage_sweep.exempt_skipped) and totalled in the pass summary
    (usage_sweep.completed), so the absorbed cost stays visible."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-log"
    )
    router = await _two_session_router(
        db_session, tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    exempt_types: list[list[str]] = []

    def dispatch(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/sessions/sesn_exempt/events":
            exempt_types.append(req.url.params.get_list("types[]"))
        return router.dispatch(req)

    client = build_fake_anthropic(dispatch)

    with structlog.testing.capture_logs() as logs:
        recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.5"))

    assert exempt_types == [["span.model_request_end"]], (
        f"the exempt session's events are filtered to model calls server-side, got {exempt_types}"
    )

    assert recorded == 1, f"only the billed session's event is replayed, got {recorded}"
    skipped = [e for e in logs if e["event"] == "usage_sweep.exempt_skipped"]
    assert len(skipped) == 1, f"one skip line for the one exempt session, got {logs!r}"
    line = skipped[0]
    assert line["tenant_id"] == str(principal.tenant_id), "skip line names the tenant"
    assert line["managed_session_id"] == "sesn_exempt", "skip line names the session"
    assert line["reason"] == "mcp-internal-caller", "skip line carries the stamped reason"
    assert line["model_id"] == "claude-sonnet-4-6", "skip line names the priced model"
    assert line["model_calls"] == 2, "both model calls are counted"
    assert line["input_tokens"] == 1_200_000, "input tokens are summed"
    assert line["output_tokens"] == 120_000, "output tokens are summed"
    assert line["cost_usd"] == "5.400000", "cost is the raw price of both calls"
    assert line["would_be_debit_usd"] == "8.100000", "would-be debit applies the markup"

    summary = [e for e in logs if e["event"] == "usage_sweep.completed"]
    assert len(summary) == 1, f"one summary line per pass, got {logs!r}"
    assert summary[0]["recorded"] == 1, "summary counts replayed events"
    assert summary[0]["exempt_sessions"] == 1, "summary counts skipped exempt sessions"
    assert summary[0]["exempt_model_calls"] == 2, "summary counts their model calls"
    assert summary[0]["exempt_cost_usd"] == "5.400000", "summary totals the absorbed cost"


@pytest.mark.parametrize(
    "headers, seconds",
    [
        ({"retry-after": "180"}, 180),
        ({"retry-after": "bad"}, 60),
        ({"retry-after-ms": "90000"}, 90),
        (
            {"retry-after": (NOW + timedelta(seconds=120)).strftime("%a, %d %b %Y %H:%M:%S GMT")},
            119,
        ),
    ],
)
async def test_sweep_yields_on_first_429_and_defers_without_advancing_progress(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    headers: dict[str, str],
    seconds: int,
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="rate-limit-user"
    )
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_limited",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            ),
            _session_dict(
                session_id="sesn_later",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            ),
        ],
    )
    calls: list[str] = []
    limited = True

    def dispatch(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if limited:
            return httpx.Response(
                429,
                headers=headers,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "busy"}},
            )
        if req.url.path.endswith("/events"):
            return list_response(
                [
                    _model_request_end_dict(
                        event_id="evt_retried", input_tokens=100, output_tokens=50
                    )
                ]
            )
        return router.dispatch(req)

    client = build_fake_anthropic(dispatch)
    watermark = UsageSweepWatermark()
    with structlog.testing.capture_logs() as logs:
        assert (
            await sweep_headless_usage(
                client, db_session_factory, markup=Decimal("1"), watermark=watermark, now=NOW
            )
            == 0
        )
        assert len(calls) == 1, "no SDK retry or next session competes with admission"
        assert watermark.last_successful_start is None
        assert watermark.retry_at is not None
        assert watermark.retry_at >= NOW + timedelta(seconds=seconds)
        assert (
            await sweep_headless_usage(
                client,
                db_session_factory,
                markup=Decimal("1"),
                watermark=watermark,
                now=NOW + timedelta(seconds=30),
            )
            == 0
        )
        assert len(calls) == 1, "deferred passes make no MA calls"
        rows = (await db_session.scalars(select(UsageSweepSession))).all()
        assert all(row.last_swept_at is None for row in rows)
        limited = False
        assert (
            await sweep_headless_usage(
                client,
                db_session_factory,
                markup=Decimal("1"),
                watermark=watermark,
                now=watermark.retry_at + timedelta(seconds=1),
            )
            == 2
        )
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.ma_calls"] == [1, 4]
    assert (await db_session.scalar(select(func.count()).select_from(UsageEvent))) == 2


async def test_sweep_counts_event_pages_and_retries_partial_history_idempotently(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="paged-user"
    )
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_paged",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    fail = True

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        if req.url.params.get("page") is None:
            return httpx.Response(
                200,
                json={
                    "data": [
                        _model_request_end_dict(
                            event_id="evt_page1", input_tokens=100, output_tokens=50
                        )
                    ],
                    "next_page": "p2",
                },
            )
        if fail:
            return httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "busy"}},
            )
        return list_response(
            [_model_request_end_dict(event_id="evt_page2", input_tokens=100, output_tokens=50)]
        )

    router.add("GET", r"/v1/sessions/sesn_paged/events", events)
    client = build_fake_anthropic(router.dispatch)
    watermark = UsageSweepWatermark()
    with structlog.testing.capture_logs() as logs:
        await sweep_headless_usage(
            client, db_session_factory, markup=Decimal("1"), watermark=watermark, now=NOW
        )
        assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 1
        assert await db_session.scalar(select(UsageSweepSession.last_swept_at)) is None
        fail = False
        assert (
            await sweep_headless_usage(
                client,
                db_session_factory,
                markup=Decimal("1"),
                watermark=watermark,
                now=NOW + timedelta(minutes=2),
            )
            == 1
        )
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.ma_calls"] == [3, 3]
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TenantLedger)
            .where(TenantLedger.reason == "turn_debit")
        )
        == 2
    )


async def test_sweep_never_fetches_unowned_session_even_with_same_tenant_stamp(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="ownership-user"
    )
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_owned",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    calls: list[str] = []

    def dispatch(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.url.path != "/v1/sessions", "never list the shared workspace"
        assert "sesn_owned" in req.url.path, "another deployment's ID is never fetched"
        if req.url.path.endswith("/events"):
            return list_response([])
        return router.dispatch(req)

    with structlog.testing.capture_logs() as logs:
        await sweep_headless_usage(
            build_fake_anthropic(dispatch), db_session_factory, markup=Decimal("1"), now=NOW
        )
    assert calls == ["/v1/sessions/sesn_owned", "/v1/sessions/sesn_owned/events"]
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.completed"] == [2]


async def test_running_session_keeps_polling_and_idle_continuation_wakes_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="running-user"
    )
    shape = _session_dict(
        session_id="sesn_running",
        tenant_id=principal.tenant_id,
        account_id=principal.account_id,
        updated_at=NOW - timedelta(days=1),
    )
    shape["status"] = "running"
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    reads: list[str] = []

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        reads.append(req.url.path)
        return list_response([])

    router.add("GET", r"/v1/sessions/[^/]+/events", events)
    client = build_fake_anthropic(router.dispatch)
    watermark = UsageSweepWatermark()
    for minute in (0, 31, 62):
        await sweep_headless_usage(
            client,
            db_session_factory,
            markup=Decimal("1"),
            watermark=watermark,
            now=NOW + timedelta(minutes=minute),
        )
    assert len(reads) == 3, "a long running session is polled despite old timestamps"
    shape["status"] = "idle"
    await sweep_headless_usage(
        client,
        db_session_factory,
        markup=Decimal("1"),
        watermark=watermark,
        now=NOW + timedelta(minutes=93),
    )
    # Advance the durable activity as a follow-up does, including a send racing a pass.
    await usage_sweep_sessions.register(
        db_session, session_id="sesn_running", tenant_id=principal.tenant_id
    )
    await db_session.execute(
        update(UsageSweepSession).values(updated_at=NOW + timedelta(minutes=94))
    )
    shape["updated_at"] = (NOW + timedelta(minutes=94)).isoformat()
    before = len(reads)
    await sweep_headless_usage(
        client,
        db_session_factory,
        markup=Decimal("1"),
        watermark=watermark,
        now=NOW + timedelta(minutes=94),
    )
    assert len(reads) == before + 1


async def test_sweep_paces_each_http_request_across_passes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import usage_sweep

    principal = await make_platform_principal(
        db_session, platform="discord", external_id="paced-user"
    )
    router = MARouter()
    await _add_owned_sessions(
        router,
        db_session,
        [
            _session_dict(
                session_id="sesn_paced",
                tenant_id=principal.tenant_id,
                account_id=principal.account_id,
            )
        ],
    )
    router.add("GET", r"/v1/sessions/sesn_paced/events", lambda req, m: list_response([]))
    time = 0.0
    calls_at: list[float] = []

    async def advance(seconds: float) -> None:
        nonlocal time
        time += seconds

    def dispatch(req: httpx.Request) -> httpx.Response:
        calls_at.append(time)
        return router.dispatch(req)

    monkeypatch.setattr(usage_sweep, "_REQUEST_INTERVAL_S", 120.0)
    monkeypatch.setattr(usage_sweep, "monotonic", lambda: time)
    monkeypatch.setattr(usage_sweep, "sleep", advance)
    client = build_fake_anthropic(dispatch)
    watermark = UsageSweepWatermark()
    await sweep_headless_usage(
        client, db_session_factory, markup=Decimal("1"), watermark=watermark, now=NOW
    )
    await sweep_headless_usage(
        client,
        db_session_factory,
        markup=Decimal("1"),
        watermark=watermark,
        now=NOW + timedelta(minutes=31),
    )
    assert calls_at == [0.0, 120.0, 240.0, 360.0], (
        "every page/retrieval is paced, even across passes"
    )


@pytest.mark.parametrize("totals_visible", [False, True])
async def test_sweep_unsettled_totals_survive_cutoff_and_retry_delayed_events(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    totals_visible: bool,
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="late-user"
    )
    router = MARouter()
    shape = _session_dict(
        session_id="sesn_late", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    shape["usage"] = {"input_tokens": 100, "output_tokens": 50} if totals_visible else {}
    await _add_owned_sessions(router, db_session, [shape])
    late = False

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        return list_response(
            [_model_request_end_dict(event_id="evt_late", input_tokens=100, output_tokens=50)]
            if late
            else []
        )

    router.add("GET", r"/v1/sessions/sesn_late/events", events)
    client = build_fake_anthropic(router.dispatch)
    await sweep_headless_usage(
        client, db_session_factory, markup=Decimal("1"), watermark=UsageSweepWatermark(), now=NOW
    )
    assert await db_session.scalar(select(UsageSweepSession.unsettled)) is True
    late = True
    shape["usage"] = {"input_tokens": 100, "output_tokens": 50}
    assert (
        await sweep_headless_usage(
            client,
            db_session_factory,
            markup=Decimal("1"),
            watermark=UsageSweepWatermark(),
            now=NOW + timedelta(hours=3),
        )
        == 1
    )
    assert await db_session.scalar(select(UsageSweepSession.unsettled)) is False
    assert (
        await sweep_headless_usage(
            client,
            db_session_factory,
            markup=Decimal("1"),
            watermark=UsageSweepWatermark(),
            now=NOW + timedelta(hours=4),
        )
        == 0
    )
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 1


@pytest.mark.parametrize(
    "protection",
    [
        "none",
        "resumable",
        "live-thread",
        "open-mcp",
        "running",
        "unfinished",
        "usage-missing",
        "usage-unavailable",
        "recent-activity",
        "recent-finish",
    ],
)
async def test_sweep_archives_only_finished_nonresumable_sessions_after_continuity_window(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    protection: str,
) -> None:
    from daimon.core._models import GitHubAppSessionVault, ThreadSession

    principal = await make_platform_principal(
        db_session, platform="discord", external_id="archive-user"
    )
    shape = _session_dict(
        session_id="sesn_finished",
        tenant_id=principal.tenant_id,
        account_id=principal.account_id,
        updated_at=NOW - timedelta(hours=3),
    )
    if protection == "running":
        shape["status"] = "running"
    if protection == "usage-missing":
        shape["usage"] = {"input_tokens": 100}
    if protection == "usage-unavailable":
        shape["usage"] = {}
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    await db_session.execute(
        update(UsageSweepSession).values(
            updated_at=NOW - timedelta(hours=1 if protection == "recent-activity" else 3),
            finished_at=(
                None
                if protection == "unfinished"
                else NOW - timedelta(hours=1 if protection == "recent-finish" else 3)
            ),
            last_swept_at=NOW - timedelta(hours=3),
            remote_status="idle",
            unsettled=protection in ("running", "usage-missing"),
            resumable=protection == "resumable",
        )
    )
    if protection == "live-thread":
        db_session.add(
            ThreadSession(
                tenant_id=principal.tenant_id,
                platform="discord",
                thread_id="thread_live",
                ma_session_id=shape["id"],
                status="live",
                updated_at=NOW - timedelta(hours=3),
            )
        )
    if protection == "open-mcp":
        db_session.add(
            GitHubAppSessionVault(
                session_id=shape["id"],
                tenant_id=principal.tenant_id,
                vault_id="vlt_protected",
                is_unmapped=True,
                is_mcp=True,
                last_started_at=NOW - timedelta(hours=3),
            )
        )
    await db_session.flush()
    calls: list[str] = []

    def dispatch(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.url.path.endswith("/archive"):
            return httpx.Response(200, json={**shape, "archived_at": NOW.isoformat()})
        if req.url.path.endswith("/events"):
            return list_response([])
        return router.dispatch(req)

    with structlog.testing.capture_logs() as logs:
        await sweep_headless_usage(
            build_fake_anthropic(dispatch),
            db_session_factory,
            markup=Decimal("1"),
            watermark=UsageSweepWatermark(),
            now=NOW,
        )
    if protection == "none":
        assert calls == [
            "GET /v1/sessions/sesn_finished",
            "GET /v1/sessions/sesn_finished/events",
            "POST /v1/sessions/sesn_finished/archive",
        ]
        assert await db_session.scalar(select(UsageSweepSession.archived_at)) == NOW
        assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.completed"] == [3]
    else:
        assert not any("archive" in call for call in calls)
        assert await db_session.scalar(select(UsageSweepSession.archived_at)) is None
        if protection in ("resumable", "live-thread", "open-mcp", "unfinished"):
            assert calls == [], "protected old history does not consume sweep requests"


async def test_sweep_continuation_racing_replay_stays_unsettled_and_protected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="racing-continuation"
    )
    shape = _session_dict(
        session_id="sesn_racing", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    await db_session.execute(
        update(UsageSweepSession).values(
            updated_at=NOW - timedelta(hours=3),
            finished_at=NOW - timedelta(hours=3),
            last_swept_at=NOW - timedelta(hours=3),
            remote_status="idle",
            unsettled=False,
            resumable=False,
        )
    )

    async def dispatch(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/events"):
            async with db_session_factory.begin() as db:
                await usage_sweep_sessions.register(
                    db, session_id=shape["id"], tenant_id=principal.tenant_id
                )
                await db.execute(
                    update(UsageSweepSession).values(updated_at=NOW - timedelta(seconds=1))
                )
            return list_response([])
        assert not req.url.path.endswith("/archive"), "a continued handle must not be archived"
        return router.dispatch(req)

    await sweep_headless_usage(
        build_fake_anthropic(dispatch),
        db_session_factory,
        markup=Decimal("1"),
        watermark=UsageSweepWatermark(),
        now=NOW,
    )
    assert await db_session.scalar(select(UsageSweepSession.unsettled)) is True
    assert await db_session.scalar(select(UsageSweepSession.resumable)) is True
    assert await db_session.scalar(select(UsageSweepSession.finished_at)) is None


async def test_archive_pacing_wait_does_not_hold_session_send_fence(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from daimon.core import usage_sweep

    principal = await make_platform_principal(
        db_session, platform="discord", external_id="archive-pacing"
    )
    shape = _session_dict(
        session_id="sesn_paced_archive",
        tenant_id=principal.tenant_id,
        account_id=principal.account_id,
    )
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    await db_session.execute(
        update(UsageSweepSession).values(
            updated_at=NOW - timedelta(hours=3),
            finished_at=NOW - timedelta(hours=3),
            last_swept_at=NOW - timedelta(hours=3),
            remote_status="idle",
            unsettled=False,
            resumable=False,
        )
    )
    in_fence = False
    time = 0.0
    calls: list[float] = []

    @asynccontextmanager
    async def fence(*args: Any, **kwargs: Any):
        nonlocal in_fence
        in_fence = True
        try:
            yield
        finally:
            in_fence = False

    async def advance(seconds: float) -> None:
        nonlocal time
        assert not in_fence, "background pacing must not block interactive sends"
        time += seconds

    def dispatch(req: httpx.Request) -> httpx.Response:
        calls.append(time)
        if req.url.path.endswith("/archive"):
            assert in_fence, "actual archive must serialize with sends"
            return httpx.Response(200, json={**shape, "archived_at": NOW.isoformat()})
        if req.url.path.endswith("/events"):
            return list_response([])
        return router.dispatch(req)

    monkeypatch.setattr(usage_sweep, "session_mutation_fence", fence)
    monkeypatch.setattr(usage_sweep, "_REQUEST_INTERVAL_S", 120.0)
    monkeypatch.setattr(usage_sweep, "monotonic", lambda: time)
    monkeypatch.setattr(usage_sweep, "sleep", advance)
    await sweep_headless_usage(
        build_fake_anthropic(dispatch),
        db_session_factory,
        markup=Decimal("1"),
        watermark=UsageSweepWatermark(),
        now=NOW,
    )
    assert calls == [0.0, 120.0, 240.0]


async def test_sweep_does_not_import_synthetic_tool_charge_session_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="synthetic-charge-user"
    )
    for prefix in ("classifier", "gemini", "thread-naming"):
        db_session.add(
            UsageEvent(
                tenant_id=principal.tenant_id,
                managed_session_id=f"{prefix}:charge",
                event_id=f"evt_{prefix}",
                occurred_at=NOW,
            )
        )
    await db_session.flush()
    with structlog.testing.capture_logs() as logs:
        await sweep_headless_usage(
            build_fake_anthropic(MARouter().dispatch),
            db_session_factory,
            markup=Decimal("1"),
            watermark=UsageSweepWatermark(),
            now=NOW,
        )
    assert await db_session.scalar(select(func.count()).select_from(UsageSweepSession)) == 0
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.completed"] == [0]


@pytest.mark.fresh_schema
@pytest.mark.parametrize("failure", ["retrieve", "events", "page", "database"])
async def test_bad_session_does_not_prevent_later_sessions_from_being_billed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    failure: str,
) -> None:
    principal = await make_platform_principal(db_session, platform="discord", external_id="poison")
    router = MARouter()
    sessions = [
        _session_dict(
            session_id=sid, tenant_id=principal.tenant_id, account_id=principal.account_id
        )
        for sid in ("sesn_a_bad", "sesn_z_good", "sesn_zz_good")
    ]
    # Override the bad retrieve before the generic route is installed.
    if failure == "retrieve":
        router.add("GET", r"/v1/sessions/sesn_a_bad", lambda req, m: httpx.Response(500))
    await _add_owned_sessions(router, db_session, sessions)
    if failure == "database":
        from sqlalchemy import text

        await db_session.execute(
            text(
                "ALTER TABLE usage_events ADD CONSTRAINT reject_bad_usage "
                "CHECK (managed_session_id <> 'sesn_a_bad')"
            )
        )
    await db_session.commit()
    requests: list[str] = []

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        sid = req.url.path.split("/")[-2]
        requests.append(sid)
        if sid == "sesn_a_bad":
            if failure == "events" or (failure == "page" and req.url.params.get("page")):
                return httpx.Response(500)
            if failure == "page":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            _model_request_end_dict(
                                event_id="evt_partial", input_tokens=10, output_tokens=5
                            )
                        ],
                        "next_page": "bad_page",
                    },
                )
        return list_response(
            [_model_request_end_dict(event_id=f"evt_{sid}", input_tokens=100, output_tokens=50)]
        )

    router.add("GET", r"/v1/sessions/[^/]+/events", events)
    client = build_fake_anthropic(router.dispatch)
    for attempt in range(6):
        await sweep_headless_usage(
            client,
            db_session_factory,
            markup=Decimal("1"),
            now=NOW + timedelta(minutes=31 * attempt),
        )
    rows = (await db_session.scalars(select(UsageEvent.managed_session_id))).all()
    assert {"sesn_z_good", "sesn_zz_good"} <= set(rows), "a poison candidate cannot block billing"
    bad = await db_session.get(UsageSweepSession, "sesn_a_bad", populate_existing=True)
    assert bad is not None and bad.unsettled, "failed billing remains queued"
    assert bad.last_swept_at == NOW + timedelta(minutes=155), "failed candidates advance progress"
    assert bad.last_error in {"InternalServerError", "IntegrityError"}, (
        "record a sanitized error class"
    )
    assert bad.retry_at == NOW + timedelta(minutes=185), "retry after bounded backoff"
    assert bad.terminal_reason is None, "transient errors must not be terminally settled"


@pytest.mark.parametrize("metadata", ["missing", "invalid", "mismatch"])
async def test_invalid_ownership_is_quarantined_until_new_activity(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    metadata: str,
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="quarantine"
    )
    shape = _session_dict(
        session_id="sesn_skip", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    if metadata == "missing":
        del shape["metadata"][MA_METADATA_KEY_TENANT]
    else:
        shape["metadata"][MA_METADATA_KEY_TENANT] = (
            "broken" if metadata == "invalid" else str(uuid.uuid4())
        )
    router.add("GET", r"/v1/sessions/sesn_skip/events", lambda req, m: list_response([]))
    client = build_fake_anthropic(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        for hour in (0, 3, 30):
            await sweep_headless_usage(
                client, db_session_factory, markup=Decimal("1"), now=NOW + timedelta(hours=hour)
            )
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.ma_calls"] == [1, 0, 0], (
        "unsafe ownership must not consume permanent polling slots"
    )
    row = await db_session.get(UsageSweepSession, "sesn_skip", populate_existing=True)
    assert row is not None and not row.unsettled and row.terminal_reason, (
        "record a terminal skip reason"
    )
    shape["metadata"][MA_METADATA_KEY_TENANT] = str(principal.tenant_id)
    await usage_sweep_sessions.register(
        db_session, session_id="sesn_skip", tenant_id=principal.tenant_id
    )
    await sweep_headless_usage(
        client, db_session_factory, markup=Decimal("1"), now=NOW + timedelta(hours=31)
    )
    await db_session.refresh(row)
    assert row.terminal_reason is None and row.last_swept_at == NOW + timedelta(hours=31), (
        "a continuation requeues ownership"
    )


@pytest.mark.parametrize("totals", [None, 101])
async def test_idle_coverage_gap_has_bounded_repair_and_cannot_archive(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    totals: int | None,
) -> None:
    principal = await make_platform_principal(db_session, platform="discord", external_id="gap")
    shape = _session_dict(
        session_id="sesn_gap", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    shape["usage"] = {"input_tokens": totals, "output_tokens": 50}
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    await db_session.execute(
        update(UsageSweepSession).values(
            resumable=False,
            updated_at=NOW - timedelta(days=1),
            finished_at=NOW - timedelta(days=1),
        )
    )
    router.add(
        "GET",
        r"/v1/sessions/sesn_gap/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_gap", input_tokens=100, output_tokens=50)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        for attempt in range(6):
            await sweep_headless_usage(
                client,
                db_session_factory,
                markup=Decimal("1"),
                now=NOW + timedelta(minutes=31 * attempt),
            )
    row = await db_session.get(UsageSweepSession, "sesn_gap", populate_existing=True)
    assert row is not None and not row.unsettled and row.terminal_reason == "coverage_gap", (
        "stop endless gap reads after three repairs without progress"
    )
    assert row.archived_at is None, "quarantined usage is never eligible for archive"
    assert not await usage_sweep_sessions.can_archive(db_session, session_id="sesn_gap", now=NOW), (
        "archive guard checks quarantine independently"
    )
    assert [e["ma_calls"] for e in logs if e["event"] == "usage_sweep.ma_calls"] == [
        2,
        2,
        2,
        2,
        0,
        0,
    ], "coverage gaps have a finite request budget"
    assert len([e for e in logs if e["event"] == "usage_sweep.coverage_gap"]) == 1, (
        "emit an actionable warning"
    )
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 1, (
        "repair reads never double debit"
    )


async def test_unmetered_recent_activity_precedes_inline_legacy_backlog(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="priority"
    )
    router = MARouter()
    shapes = [
        _session_dict(
            session_id=sid, tenant_id=principal.tenant_id, account_id=principal.account_id
        )
        for sid in ("sesn_a_legacy", "sesn_b_inline", "sesn_y_headless", "sesn_z_mcp")
    ]
    await _add_owned_sessions(router, db_session, shapes)
    await db_session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id.in_(["sesn_y_headless", "sesn_z_mcp"]))
        .values(priority=True)
    )
    await db_session.execute(
        update(UsageSweepSession)
        .where(UsageSweepSession.session_id == "sesn_z_mcp")
        .values(last_swept_at=NOW - timedelta(hours=1), updated_at=NOW + timedelta(seconds=1))
    )
    reads: list[str] = []
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: reads.append(req.url.path) or list_response([]),
    )
    await sweep_headless_usage(
        build_fake_anthropic(router.dispatch), db_session_factory, markup=Decimal("1"), now=NOW
    )
    assert reads == ["/v1/sessions/sesn_z_mcp/events", "/v1/sessions/sesn_y_headless/events"], (
        "continued MCP and billed headless usage precede old inline repairs"
    )


async def test_checkpoint_resumes_after_restart_and_preserves_equal_timestamp_events(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(db_session, platform="discord", external_id="cursor")
    shape = _session_dict(
        session_id="sesn_cursor", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    shape["usage"] = {"input_tokens": 200, "output_tokens": 100}
    old = _model_request_end_dict(event_id="evt_old", input_tokens=100, output_tokens=50)
    boundary = _model_request_end_dict(event_id="evt_boundary", input_tokens=100, output_tokens=50)
    boundary["processed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    new = dict(boundary, id="evt_equal")
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    queries: list[dict[str, str]] = []

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        queries.append(dict(req.url.params))
        if req.url.params.get("created_at[gte]"):
            return list_response([boundary, new])
        return list_response([old, boundary])

    router.add("GET", r"/v1/sessions/sesn_cursor/events", events)
    client = build_fake_anthropic(router.dispatch)
    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1"), now=NOW)
    shape["usage"] = {"input_tokens": 300, "output_tokens": 150}
    await usage_sweep_sessions.register(
        db_session, session_id="sesn_cursor", tenant_id=principal.tenant_id
    )
    assert (
        await sweep_headless_usage(
            client, db_session_factory, markup=Decimal("1"), now=NOW + timedelta(minutes=1)
        )
        == 1
    ), "restart bills only a new boundary event"
    assert queries[0]["limit"] == "1000" and "created_at[gte]" not in queries[0], (
        "first read uses the large page size"
    )
    assert datetime.fromisoformat(queries[1]["created_at[gte]"]) == NOW + timedelta(seconds=1), (
        "later reads use the durable inclusive cursor"
    )
    row = await db_session.get(UsageSweepSession, "sesn_cursor", populate_existing=True)
    assert row is not None and row.checkpoint["input_tokens"] == 300 and not row.unsettled, (
        "coverage combines old pages and new events without duplicate tokens"
    )
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 3, (
        "equal timestamps cannot lose or double debit a model call"
    )


async def test_coverage_repair_finds_late_event_before_saved_cursor(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="late-cursor"
    )
    shape = _session_dict(
        session_id="sesn_repair", tenant_id=principal.tenant_id, account_id=principal.account_id
    )
    shape["usage"] = {"input_tokens": 201, "output_tokens": 100}
    first = _model_request_end_dict(event_id="evt_first", input_tokens=100, output_tokens=50)
    first["processed_at"] = (NOW + timedelta(seconds=10)).isoformat()
    late = _model_request_end_dict(event_id="evt_earlier", input_tokens=101, output_tokens=50)
    late["processed_at"] = (NOW + timedelta(seconds=5)).isoformat()
    router = MARouter()
    await _add_owned_sessions(router, db_session, [shape])
    requests: list[httpx.Request] = []

    def events(req: httpx.Request, match: Any) -> httpx.Response:
        requests.append(req)
        return list_response([first] if len(requests) == 1 else [late, first])

    router.add("GET", r"/v1/sessions/sesn_repair/events", events)
    client = build_fake_anthropic(router.dispatch)
    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1"), now=NOW)
    assert await db_session.scalar(select(UsageSweepSession.unsettled)) is True, (
        "insufficient coverage stays queued"
    )
    assert (
        await sweep_headless_usage(
            client, db_session_factory, markup=Decimal("1"), now=NOW + timedelta(minutes=31)
        )
        == 1
    ), "repair bills the late event once"
    assert "created_at[gte]" not in requests[1].url.params, (
        "repair must look behind the saved cursor"
    )
    row = await db_session.get(UsageSweepSession, "sesn_repair", populate_existing=True)
    assert row is not None and not row.unsettled and row.checkpoint["input_tokens"] == 201, (
        "repair rebuilds totals without adding the old page twice"
    )
    assert await db_session.scalar(select(func.count()).select_from(UsageEvent)) == 2, (
        "historical replay remains idempotent"
    )

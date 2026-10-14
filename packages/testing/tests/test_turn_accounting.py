"""Real PostgreSQL revision, ownership, unknown-spend and READ ONLY proofs."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
import pytest_asyncio
from daimon.core import accounting_outbox
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import mux_state
from daimon.core.stores.turn_usage import read_accounting_revisions
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from daimon.testing.turn_accounting import (
    RevisionRateAttribution,
    TurnAccountingReader,
    TurnUsageInventory,
)
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

TENANT = uuid.UUID(int=8)
NOW = datetime(2026, 10, 10, tzinfo=UTC)


@dataclass(frozen=True)
class Account:
    reader: TurnAccountingReader
    provider: str

    def usage(self, revision=1, output=100, **changes):
        values = dict(
            id="native-usage",
            revision=revision,
            native_revision=f"native-{revision}",
            session=self.reader.native_session,
            turn_id="root",
            model=ModelRef(provider=self.provider, id="model"),
            grain="model_request" if self.provider == "anthropic" else "turn",
            basis="increment" if self.provider == "anthropic" else "cumulative",
            input_tokens=100,
            input_cached_tokens=20,
            input_cache_write_tokens=10 if self.provider == "anthropic" else None,
            output_tokens=output,
            output_reasoning_tokens=7,
            native_meter={"native_provider": self.provider, "raw": {"counter": output}},
            completeness="measured" if output is not None else "partial",
            observed_at=NOW,
        )
        return UsageObservation(**(values | changes))

    def rate(self, revision=1, **changes):
        price = ProviderPrice(
            provider=self.provider,
            model="model",
            checked_on=date(2026, 10, 10),
            input=Decimal(2),
            output=Decimal(10),
            cache_read=Decimal(1),
            cache_write=Decimal(3) if self.provider == "anthropic" else None,
        )
        return RevisionRateAttribution(
            **(
                dict(
                    observation_id="native-usage",
                    revision=revision,
                    price=price,
                    rate_evidence_id="controlled-dated-rate",
                    infrastructure_policy="not_applicable"
                    if self.provider == "anthropic"
                    else "measured",
                    infrastructure_usd=Decimal(0)
                    if self.provider == "anthropic"
                    else Decimal("0.002"),
                    infrastructure_seconds=None if self.provider == "anthropic" else Decimal(12),
                    infrastructure_evidence_id="controlled-infrastructure-policy",
                )
                | changes
            )
        )

    def seal(self, *observations):
        return TurnUsageInventory(
            binding_id=self.reader.binding.id,
            session=self.reader.native_session,
            root_turn_id="root",
            revisions=tuple((v.id, v.revision) for v in observations),
            evidence_id="independent-native-terminal-or-exhausted-walk",
            kind="sealed_request_walk"
            if self.provider == "anthropic"
            else "terminal_native_inventory",
        )

    async def record(self, observation, *, settle=True, shared_infrastructure=False, rate=None):
        rate = rate or self.rate(observation.revision)
        async with self.reader.sessions() as session, session.begin():
            if not settle:
                return await mux_state.record_usage(session, self.reader.binding.id, observation)
            return await accounting_outbox.record_observation_usage(
                session,
                binding_id=self.reader.binding.id,
                observation=observation,
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=ModelRates(
                    float(rate.price.input),
                    float(rate.price.output),
                    float(rate.price.cache_write or 0),
                    float(rate.price.cache_read),
                ),
                provider_price=rate.price if self.provider != "anthropic" else None,
                infrastructure_usd=Decimal(0) if shared_infrastructure else rate.infrastructure_usd,
                channel_id="channel",
                billing_grain=observation.grain,
            )


@pytest_asyncio.fixture(params=["anthropic", "openai", "gemini"])
async def account(request, db_engine: AsyncEngine, db_clean: None) -> AsyncIterator[Account]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        pytest.skip("requires owned local test DB or registered isolated GitHub Actions CI schema")
    scope = Scope(
        tenant_id=str(TENANT), account_id="account", principal_id="user", authorization_id="host"
    )
    native_session = ResourceRef(
        id="session",
        kind="session",
        provider=request.param,
        account_scope_id="workspace",
        tenant_id=str(TENANT),
        account_id="account",
    )
    binding = ProviderBinding(
        id="binding",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="channel"),
            thread_id="thread",
        ),
        provider=request.param,
        profile="fixture-profile",
        generation=1,
        config_revision=1,
        native_refs={"session": "session"},
    )
    async with sessions() as session, session.begin():
        await make_tenant(session, id=TENANT)
        await mux_state.put_binding(session, binding, expected_generation=0)
    yield Account(
        TurnAccountingReader(sessions, scope, binding, native_session, "root"), request.param
    )


async def test_signed_revisions_native_meter_and_complete_exact_totals(account: Account):
    values = tuple(
        account.usage(revision, output) for revision, output in enumerate((100, 120, 110), 1)
    )
    for value in values:
        assert await account.record(value)
    async with account.reader.sessions() as session:
        before = await read_accounting_revisions(
            session, tenant_id=TENANT, binding=account.reader.binding
        )
    with localcontext() as context:
        context.prec = 6
        report = await account.reader.snapshot(
            inventory=account.seal(values[-1]),
            attributions=tuple(account.rate(v.revision) for v in values),
        )
    async with account.reader.sessions() as session:
        after = await read_accounting_revisions(
            session, tenant_id=TENANT, binding=account.reader.binding
        )
    assert before == after
    assert (
        report.complete
        and report.coverage_complete
        and report.pricing_complete
        and report.settlement_complete
    )
    assert report.issues == ()
    assert tuple(item.observation for item in report.revisions) == values
    assert [item.latest for item in report.revisions] == [False, False, True]
    assert [item.selected for item in report.revisions] == [False, False, True]
    assert [item.observation.native_revision for item in report.revisions] == [
        "native-1",
        "native-2",
        "native-3",
    ]
    assert report.revisions[-1].observation.native_meter == values[-1].native_meter
    assert report.revisions[-1].ledger_delta_usd == Decimal("0.000100")
    expected = Decimal("0.00129") if account.provider == "anthropic" else Decimal("0.00328")
    assert report.total_usd == expected
    assert report.expected_ledger_usd == report.ledger_usd == expected
    assert report.revisions[-1].infrastructure_seconds == (
        None if account.provider == "anthropic" else 12
    )
    # These accounting invariants also run under python -O.
    if (
        before != after
        or not report.complete
        or report.issues
        or tuple(item.observation for item in report.revisions) != values
        or report.total_usd != expected
        or report.expected_ledger_usd != expected
        or report.ledger_usd != expected
        or report.revisions[-1].ledger_delta_usd != Decimal("0.000100")
    ):
        raise RuntimeError("optimized revision/precision/read-only accounting invariant failed")


@pytest.mark.parametrize(
    "field",
    [
        "price",
        "rate_evidence_id",
        "infrastructure_usd",
        "infrastructure_policy",
        "infrastructure_evidence_id",
    ],
)
async def test_unknown_rate_or_infrastructure_never_becomes_zero(account: Account, field: str):
    value = account.usage()
    await account.record(value)
    changes = {field: None}
    if field == "infrastructure_usd":
        changes["infrastructure_policy"] = None
    rate = account.rate(**changes)
    report = await account.reader.snapshot(inventory=account.seal(value), attributions=(rate,))
    assert not report.complete and not report.pricing_complete
    assert report.total_usd is None and report.expected_ledger_usd is None
    assert report.revisions[-1].attribution == rate
    if field == "price":
        assert report.revisions[-1].model_usd is None
    if field == "infrastructure_usd":
        assert report.revisions[-1].infrastructure_usd is None
    if (
        report.complete
        or report.pricing_complete
        or report.total_usd is not None
        or (report.expected_ledger_usd is not None)
    ):
        raise RuntimeError("unknown evidence became a complete or zero-priced total")


async def test_unknown_container_time_is_retained(account: Account):
    value = account.usage()
    await account.record(value)
    rate = account.rate(infrastructure_policy="measured", infrastructure_seconds=None)
    report = await account.reader.snapshot(inventory=account.seal(value), attributions=(rate,))
    assert not report.complete and report.total_usd is None
    assert report.revisions[-1].infrastructure_seconds is None
    assert "CONTAINER_TIME_UNKNOWN" in report.revisions[-1].issues


async def test_no_inventory_or_stale_inventory_is_incomplete(account: Account):
    first, second = account.usage(), account.usage(2, 120)
    await account.record(first)
    await account.record(second)
    for seal, issue in (
        (None, "TURN_INVENTORY_UNKNOWN"),
        (account.seal(first), "TURN_INVENTORY_MISMATCH"),
    ):
        report = await account.reader.snapshot(inventory=seal, attributions=(account.rate(2),))
        assert not report.complete and not report.coverage_complete and report.total_usd is None
        assert issue in report.issues


async def test_missing_native_work_and_unattributed_work_block_closure(account: Account):
    first = account.usage()
    missing = account.usage(id="missing-work", turn_id=None)
    await account.record(first)
    await account.record(missing, settle=False)
    report = await account.reader.snapshot(
        inventory=account.seal(first), attributions=(account.rate(),)
    )
    assert not report.complete and report.total_usd is None
    assert report.unattributed == (missing,)
    assert "ROOT_ATTRIBUTION_UNKNOWN" in report.issues
    # A walk seal can bind historical spans that did not persist a turn ID,
    # without rewriting those observations or pretending their prices are known.
    joined = await account.reader.snapshot(
        inventory=account.seal(first, missing), attributions=(account.rate(),)
    )
    assert joined.coverage_complete and not joined.complete
    unbound = next(item for item in joined.revisions if item.observation.id == missing.id)
    assert unbound.observation.turn_id is None
    assert unbound.model_usd is None


async def test_model_context_must_be_independent_and_match_rate(account: Account):
    value = account.usage(model=None)
    await account.record(value, settle=False)
    unknown = await account.reader.snapshot(
        inventory=account.seal(value), attributions=(account.rate(),)
    )
    assert unknown.revisions[0].model is None and unknown.revisions[0].model_usd is None
    bound = account.rate(
        admitted_model=ModelRef(provider=account.provider, id="model"),
        model_evidence_id="admitted-request-model",
    )
    report = await account.reader.snapshot(inventory=account.seal(value), attributions=(bound,))
    assert report.pricing_complete and not report.complete
    assert report.revisions[0].model == bound.admitted_model
    assert report.revisions[0].observation.model is None
    assert "OUTBOX_NOT_SETTLED" in report.issues
    wrong = replace(bound, admitted_model=ModelRef(provider=account.provider, id="wrong"))
    report = await account.reader.snapshot(inventory=account.seal(value), attributions=(wrong,))
    assert report.revisions[0].total_usd is None
    assert "PRICE_ATTRIBUTION_MISMATCH" in report.revisions[0].issues


async def test_partial_usage_cannot_reuse_effective_known_counts(account: Account):
    await account.record(account.usage())
    value = account.usage(2, output=None)
    await account.record(value, settle=False)
    report = await account.reader.snapshot(
        inventory=account.seal(value), attributions=(account.rate(2),)
    )
    assert not report.complete and report.total_usd is None
    assert report.revisions[-1].observation.output_tokens is None
    assert report.revisions[-1].model_usd is None


async def test_pending_outbox_and_ledger_mismatch_remain_incomplete(account: Account):
    value = account.usage()
    await account.record(value, settle=False)
    pending = await account.reader.snapshot(
        inventory=account.seal(value), attributions=(account.rate(),)
    )
    assert pending.pricing_complete and pending.coverage_complete
    assert not pending.settlement_complete and not pending.complete
    assert pending.ledger_usd is None
    # Apply normal production accounting, then vary only independent controlled rates.
    async with account.reader.sessions() as session, session.begin():
        rows = await read_accounting_revisions(
            session, tenant_id=TENANT, binding=account.reader.binding
        )
        await accounting_outbox.apply_usage_outbox(
            session,
            rows.outbox[0].row,
            tenant_id=TENANT,
            platform_user_id="user",
            pricing=ModelRates(2, 10, 3, 1),
            provider_price=account.rate().price if account.provider != "anthropic" else None,
            infrastructure_usd=account.rate().infrastructure_usd,
            channel_id="channel",
            billing_grain=value.grain,
        )
    rate = account.rate()
    rate = replace(rate, price=replace(rate.price, output=Decimal(20)))
    mismatch = await account.reader.snapshot(inventory=account.seal(value), attributions=(rate,))
    assert mismatch.settlement_complete and not mismatch.complete
    assert "LEDGER_COST_MISMATCH" in mismatch.issues


async def test_reader_executes_only_unlocked_consistent_reads(
    account: Account, db_engine: AsyncEngine
):
    value = account.usage()
    await account.record(value)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    try:
        report = await account.reader.snapshot(
            inventory=account.seal(value), attributions=(account.rate(),)
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)
    assert report.complete
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert all(s.startswith(("SELECT", "SET TRANSACTION")) for s in statements)
    assert all("FOR UPDATE" not in s and "advisory" not in s.lower() for s in statements)


async def test_postgres_refuses_an_accidental_reader_write(account: Account, monkeypatch):
    async def write(session, **_kwargs):
        await session.execute(
            text("DELETE FROM tenant_ledger WHERE tenant_id = :tenant"), {"tenant": TENANT}
        )
        raise RuntimeError("database accepted an accidental reader write")

    monkeypatch.setattr("daimon.testing.turn_accounting.read_accounting_revisions", write)
    with pytest.raises(DBAPIError, match="read-only transaction"):
        await account.reader.snapshot()


async def test_scope_and_duplicate_evidence_refused(account: Account):
    value = account.usage()
    for reader_changes in (
        {"scope": Scope.platform(reason="not-a-tenant")},
        {
            "native_session": account.reader.native_session.model_copy(
                update={"account_id": "foreign"}
            )
        },
        {
            "binding": account.reader.binding.model_copy(
                update={"provider": "gemini" if account.provider != "gemini" else "openai"}
            )
        },
    ):
        with pytest.raises(ValueError):
            replace(account.reader, **reader_changes)
    with pytest.raises(ValueError, match="duplicate"):
        await account.reader.snapshot(attributions=(account.rate(), account.rate()))
    with pytest.raises(ValueError, match="another"):
        await account.reader.snapshot(
            inventory=replace(account.seal(value), root_turn_id="foreign")
        )


async def test_foreign_and_incoherent_routes_refused_before_connecting(account: Account):
    foreign = create_async_engine("postgresql+asyncpg://localhost/production")
    other = create_async_engine("postgresql+asyncpg://localhost/daimon_test_nc_N8")

    def refuse(*_args):
        raise RuntimeError("guard attempted connection")

    event.listen(foreign.sync_engine, "do_connect", refuse)
    event.listen(other.sync_engine, "do_connect", refuse)
    try:
        owned = account.reader.sessions.kw["bind"]
        for engine, reason in ((foreign, "owned local"), (other, "consistent")):
            factory = async_sessionmaker(owned, binds={object: engine})
            with pytest.raises(ValueError, match=reason):
                replace(account.reader, sessions=factory)
        # The second guard prevents mutable routing after construction.
        account.reader.sessions.configure(binds={object: foreign})
        with pytest.raises(ValueError, match="owned local"):
            await account.reader.snapshot()
    finally:
        account.reader.sessions.configure(binds={})
        await foreign.dispose()
        await other.dispose()


async def test_other_root_and_tenant_never_enter_totals(account: Account):
    first = account.usage()
    other = account.usage(id="other-root", turn_id="another-root")
    await account.record(first)
    await account.record(other)
    report = await account.reader.snapshot(
        inventory=account.seal(first), attributions=(account.rate(),)
    )
    assert report.complete and len(report.revisions) == 1
    assert report.revisions[0].observation == first
    async with account.reader.sessions() as session:
        with pytest.raises(ValueError, match="requested tenant"):
            await read_accounting_revisions(
                session, tenant_id=uuid.UUID(int=99), binding=account.reader.binding
            )


async def test_price_postdating_usage_and_unverified_price_are_unknown(account: Account):
    value = account.usage()
    await account.record(value)
    rate = account.rate()
    for price, issue in (
        (replace(rate.price, checked_on=date(2026, 10, 11)), "PRICE_POSTDATES_USAGE"),
        (replace(rate.price, verified=False), "PRICE_UNKNOWN"),
    ):
        report = await account.reader.snapshot(
            inventory=account.seal(value), attributions=(replace(rate, price=price),)
        )
        assert not report.complete and report.total_usd is None
        assert issue in report.revisions[0].issues


async def test_disjoint_native_work_adds_model_cost_and_shared_runtime_once(account: Account):
    root = account.usage()
    child = account.usage(
        id="native-child", thread_id="native-subagent" if account.provider == "openai" else None
    )
    await account.record(root)
    await account.record(child, shared_infrastructure=True)
    rate = account.rate(
        observation_id=child.id,
        infrastructure_policy="not_applicable",
        infrastructure_usd=Decimal(0),
        infrastructure_seconds=None,
        infrastructure_evidence_id="native-inventory-shared-runtime-charged-on-root",
    )
    report = await account.reader.snapshot(
        inventory=account.seal(root, child), attributions=(account.rate(), rate)
    )
    assert report.complete and len(report.revisions) == 2
    expected = Decimal("0.00238") if account.provider == "anthropic" else Decimal("0.00436")
    assert report.total_usd == report.expected_ledger_usd == report.ledger_usd == expected
    assert sum(item.infrastructure_usd for item in report.revisions) == (
        0 if account.provider == "anthropic" else Decimal("0.002")
    )
    missing_child = await account.reader.snapshot(
        inventory=account.seal(root), attributions=(account.rate(),)
    )
    assert not missing_child.complete and "TURN_INVENTORY_MISMATCH" in missing_child.issues


async def test_native_session_total_is_not_a_turn_allocation(account: Account):
    value = account.usage(grain="session")
    await account.record(value, settle=False)
    report = await account.reader.snapshot(
        inventory=account.seal(value), attributions=(account.rate(),)
    )
    assert not report.complete and report.total_usd is None
    assert "SESSION_TO_ROOT_ATTRIBUTION_UNSUPPORTED" in report.issues


async def test_covered_aggregate_is_retained_but_never_certified(account: Account):
    leaf = account.usage()
    aggregate = account.usage(id="aggregate", grain="session", covers=(leaf.id,))
    await account.record(leaf)
    await account.record(aggregate, settle=False)
    report = await account.reader.snapshot(
        inventory=account.seal(leaf, aggregate), attributions=(account.rate(),)
    )
    assert not report.complete and report.total_usd is None
    assert "AGGREGATE_COVERAGE_UNSUPPORTED" in report.issues
    assert (
        next(item for item in report.revisions if item.observation.id == aggregate.id).observation
        == aggregate
    )


async def test_unrelated_rate_and_stale_binding_are_not_complete(account: Account):
    value = account.usage()
    await account.record(value)
    with pytest.raises(ValueError, match="persisted revision"):
        await account.reader.snapshot(
            inventory=account.seal(value), attributions=(account.rate(observation_id="foreign-id"),)
        )
    async with account.reader.sessions() as session, session.begin():
        await mux_state.put_binding(
            session,
            account.reader.binding.model_copy(update={"generation": 2}),
            expected_generation=1,
        )
    report = await account.reader.snapshot(
        inventory=account.seal(value), attributions=(account.rate(),)
    )
    assert not report.complete and "BINDING_NOT_CURRENT" in report.issues


async def test_fractional_prices_stay_exact_at_low_ambient_precision(account: Account):
    value = account.usage()
    rate = account.rate()
    price = replace(
        rate.price,
        input=Decimal("2.123456789012345"),
        output=Decimal("10.987654321098765"),
        cache_read=Decimal("0.987654321098765"),
    )
    rate = replace(rate, price=price)
    await account.record(value, rate=rate)
    with localcontext() as context:
        context.prec = 80
        uncached = 70 if account.provider == "anthropic" else 80
        expected = (
            Decimal(uncached) * price.input
            + Decimal(20) * price.cache_read
            + Decimal(100) * price.output
            + (Decimal(30) if account.provider == "anthropic" else 0)
        ) / Decimal(1_000_000) + rate.infrastructure_usd
    with localcontext() as context:
        context.prec = 6
        report = await account.reader.snapshot(inventory=account.seal(value), attributions=(rate,))
    if not report.complete or report.total_usd != expected:
        raise RuntimeError("fractional rate total rounded by ambient precision")
    if report.revisions[-1].model_usd + rate.infrastructure_usd != expected:
        raise RuntimeError("normalized model/infrastructure attribution changed exact cost")


async def test_shared_positive_infrastructure_evidence_cannot_be_charged_twice(account: Account):
    if account.provider == "anthropic":
        with pytest.raises(ValueError, match="explicit zero"):
            account.rate(infrastructure_usd=Decimal("0.002"))
        return
    root = account.usage()
    child = account.usage(id="child")
    await account.record(root)
    await account.record(child)
    child_rate = account.rate(observation_id="child")
    report = await account.reader.snapshot(
        inventory=account.seal(root, child), attributions=(account.rate(), child_rate)
    )
    if (
        report.complete
        or report.total_usd is not None
        or "INFRASTRUCTURE_COVERAGE_OVERLAP" not in report.issues
    ):
        raise RuntimeError("duplicate shared infrastructure certified as exact spend")


async def test_public_store_never_autoflushes_pending_caller_objects(account: Account):
    from daimon.core._models import TenantLedger

    pending = TenantLedger(
        tenant_id=TENANT,
        delta_usd=Decimal(1),
        reason="manual_credit",
        idempotency_key="must-remain-unflushed",
    )
    async with account.reader.sessions() as session:
        session.add(pending)
        await read_accounting_revisions(session, tenant_id=TENANT, binding=account.reader.binding)
        if pending not in session.new or not session.autoflush:
            raise RuntimeError("read API flushed pending caller objects or changed session policy")

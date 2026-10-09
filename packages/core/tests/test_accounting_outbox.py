"""Real Postgres money-path proofs for usage revisions and restart recovery."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
import pytest_asyncio
from daimon.core import accounting_outbox
from daimon.core.pricing import ModelRates
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import TenantLedgerRow, UsageEventRow
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.usage_recording import TurnLedgerReason, record_turn_usage
from daimon.testing.factories import make_tenant
from mux.conformance.fixtures import c12
from mux.conformance.reference import create
from mux.conformance.runner import ConformanceFailure
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.usage_ledger import OutboxRow
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TENANT = uuid.UUID(int=8)
RATES = ModelRates(input=2, output=10, cache_write=3, cache_read=1)


def observation(
    revision: int = 1,
    output: int | None = 100,
    *,
    event_id: str = "event",
    input_tokens: int | None = 0,
    cached: int | None = 0,
    written: int | None = 0,
) -> UsageObservation:
    return UsageObservation(
        id=event_id,
        revision=revision,
        session=ResourceRef(
            id="session",
            kind="session",
            provider="anthropic",
            account_scope_id="workspace",
            tenant_id=str(TENANT),
        ),
        model=ModelRef(provider="anthropic", id="model"),
        grain="model_request",
        basis="increment",
        input_tokens=input_tokens,
        output_tokens=output,
        input_cached_tokens=cached,
        input_cache_write_tokens=written,
        completeness="partial" if output is None or input_tokens is None else "measured",
        observed_at=NOW,
    )


@dataclass(frozen=True)
class Account:
    sessions: async_sessionmaker[AsyncSession]

    @property
    def store(self) -> PostgresStateStore:
        return PostgresStateStore(self.sessions)

    async def queue(self, usage: UsageObservation) -> OutboxRow:
        row = await self.store.record_usage("binding", usage)
        assert row is not None
        return row

    async def apply(
        self,
        row: OutboxRow,
        *,
        rates: ModelRates | None = RATES,
        billing_grain: accounting_outbox.BillingGrain = "model_request",
    ) -> bool:
        async with self.sessions() as session, session.begin():
            return await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=rates,
                channel_id="channel",
                billing_grain=billing_grain,
            )

    async def record(
        self,
        usage: UsageObservation,
        *,
        billing_grain: accounting_outbox.BillingGrain = "model_request",
    ) -> bool:
        async with self.sessions() as session, session.begin():
            return await accounting_outbox.record_observation_usage(
                session,
                binding_id="binding",
                observation=usage,
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=RATES,
                channel_id="channel",
                billing_grain=billing_grain,
            )

    async def rows(self) -> tuple[Sequence[UsageEventRow], Sequence[TenantLedgerRow]]:
        async with self.sessions() as session:
            return (
                await usage_events.list_for_tenant(session, tenant_id=TENANT),
                await tenant_ledger.list_for_tenant(session, tenant_id=TENANT),
            )


@pytest_asyncio.fixture
async def account(db_engine: AsyncEngine, db_clean: None) -> AsyncIterator[Account]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await make_tenant(session, id=TENANT)
    binding = ProviderBinding(
        id="binding",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
            thread_id="thread",
        ),
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": "session"},
        generation=1,
        config_revision=1,
    )
    await PostgresStateStore(sessions).put_binding(binding, expected_generation=0)
    yield Account(sessions)


async def test_c07_null_100_120_110_is_signed_once_and_channel_spend_is_110(
    account: Account,
) -> None:
    for revision, output in enumerate((None, 100, 120, 110), 1):
        assert await account.record(observation(revision, output))
    assert not await account.record(observation(2, 100))
    usage, ledger = await account.rows()
    assert len(usage) == 1 and usage[0].output_tokens == 110
    assert usage[0].observation_revision == 4
    assert {r.idempotency_key: r.delta_usd for r in ledger} == {
        "adjust:binding:event:2": Decimal("-0.001000"),
        "adjust:binding:event:3": Decimal("-0.000200"),
        "adjust:binding:event:4": Decimal("0.000100"),
    }
    async with account.sessions() as session:
        assert await tenant_ledger.get_balance(session, tenant_id=TENANT) == Decimal("-0.001100")
        assert await tenant_ledger.get_channel_spend(
            session,
            tenant_id=TENANT,
            channel_id="channel",
            since=NOW,
            until=NOW + timedelta(days=1),
        ) == Decimal("0.001100")
        assert (
            await session.scalar(
                text(
                    "SELECT applied->'tokens'->'output_tokens' FROM usage_observation WHERE revision=1"
                )
            )
            is None
        )
    assert await account.store.pending_outbox() == []


async def test_c12_historical_replay_keeps_exact_rows_and_turn_key(account: Account) -> None:
    original = observation()
    await record_turn_usage(
        sessionmaker=account.sessions,
        tenant_id=TENANT,
        platform_user_id="user",
        observation=original,
        pricing=RATES,
        channel_id="channel",
    )
    before_usage, before_ledger = await account.rows()
    assert before_usage[0].observation_revision is None
    assert await account.record(original)
    for _ in range(3):
        assert not await account.record(original)
        await record_turn_usage(
            sessionmaker=account.sessions,
            tenant_id=TENANT,
            platform_user_id="user",
            observation=original,
            pricing=RATES,
            channel_id="channel",
        )
    after_usage, after_ledger = await account.rows()
    assert [r.model_dump(exclude={"observation_revision"}) for r in after_usage] == [
        r.model_dump(exclude={"observation_revision"}) for r in before_usage
    ]
    assert after_ledger == before_ledger
    assert len(after_ledger) == 1
    assert after_ledger[0].idempotency_key == "turn:session:event"
    assert after_ledger[0].delta_usd == Decimal("-0.001000")
    assert after_ledger[0].occurred_at == NOW


async def test_cache_known_before_inclusive_input_is_not_charged_twice(account: Account) -> None:
    rates = ModelRates(input=2, output=3, cache_write=4, cache_read=1)
    first = await account.queue(observation(input_tokens=None, cached=10, written=0))
    assert await account.apply(first, rates=rates)
    assert not (await account.rows())[0]
    second = await account.queue(observation(2, None, input_tokens=100, cached=None, written=None))
    assert await account.apply(second, rates=rates)
    usage, ledger = await account.rows()
    assert usage[0].input_tokens == 90 and usage[0].cache_read_input_tokens == 10
    assert usage[0].output_tokens == 100 and usage[0].observation_revision == 2
    assert {r.idempotency_key: r.delta_usd for r in ledger} == {
        "turn:session:event": Decimal("-0.000310"),
        "adjust:binding:event:2": Decimal("-0.000180"),
    }
    assert sum((r.delta_usd for r in ledger), Decimal("0")) == Decimal("-0.000490")
    async with account.sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT applied->'observation'->'output_tokens' FROM usage_observation WHERE revision=2"
                )
            )
            is None
        )


async def test_corrections_subtract_quantized_totals_instead_of_rounding_token_delta(
    account: Account,
) -> None:
    rates = ModelRates(input=0, output=0.4, cache_write=0, cache_read=0)
    for revision, output in enumerate((1, 2, 1), 1):
        assert await account.apply(await account.queue(observation(revision, output)), rates=rates)
    _, ledger = await account.rows()
    assert {r.idempotency_key: r.delta_usd for r in ledger} == {
        "turn:session:event": Decimal("0.000000"),
        "adjust:binding:event:2": Decimal("-0.000001"),
        "adjust:binding:event:3": Decimal("0.000001"),
    }
    assert sum((r.delta_usd for r in ledger), Decimal("0")) == Decimal("0.000000")


async def test_reverse_application_keeps_latest_projection_and_all_older_deltas(
    account: Account,
) -> None:
    rows = [
        await account.queue(observation(rev, value)) for rev, value in enumerate((100, 120, 110), 1)
    ]
    for row in reversed(rows):
        assert await account.apply(row)
    usage, ledger = await account.rows()
    assert usage[0].output_tokens == 110 and usage[0].observation_revision == 3
    assert len(ledger) == 3
    assert sum((r.delta_usd for r in ledger), Decimal("0")) == Decimal("-0.001100")
    for row in rows:
        assert not await account.apply(row)
    assert await account.rows() == (usage, ledger)


async def test_two_workers_claim_one_pending_row(account: Account) -> None:
    row = await account.queue(observation())
    assert sorted(await asyncio.gather(account.apply(row), account.apply(row))) == [False, True]
    usage, ledger = await account.rows()
    assert len(usage) == len(ledger) == 1
    assert ledger[0].delta_usd == Decimal("-0.001000")
    assert await account.store.pending_outbox() == []


async def test_two_workers_apply_distinct_revisions_without_rewinding(account: Account) -> None:
    first = await account.queue(observation())
    second = await account.queue(observation(2, 120))
    assert await asyncio.gather(account.apply(first), account.apply(second)) == [True, True]
    usage, ledger = await account.rows()
    assert usage[0].output_tokens == 120 and usage[0].observation_revision == 2
    assert sum((r.delta_usd for r in ledger), Decimal("0")) == Decimal("-0.001200")


@pytest.mark.parametrize("point", ["before_usage", "before_ledger", "after_ledger"])
async def test_crash_rolls_back_claim_usage_and_debit_then_restart_replays_once(
    account: Account, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    assert await account.record(observation())
    before = await account.rows()
    pending = await account.queue(observation(2, 120))

    async def fail_before_usage(session: AsyncSession, **kwargs: object) -> None:
        await session.execute(text("SELECT 1 / 0"))

    async def fail_after_usage(session: AsyncSession, **kwargs: object) -> bool:
        projected = await usage_events.list_for_tenant(session, tenant_id=TENANT)
        assert projected[0].output_tokens == 120 and projected[0].observation_revision == 2
        raise RuntimeError("fixture process crash")

    with monkeypatch.context() as patch:
        if point == "before_usage":
            patch.setattr(usage_events, "project_revision", fail_before_usage)
        elif point == "before_ledger":
            patch.setattr(tenant_ledger, "insert_entry", fail_after_usage)
        with pytest.raises((DBAPIError, RuntimeError)):
            async with account.sessions() as session, session.begin():
                await accounting_outbox.apply_usage_outbox(
                    session,
                    pending,
                    tenant_id=TENANT,
                    platform_user_id="user",
                    pricing=RATES,
                    channel_id="channel",
                )
                raise RuntimeError("fixture process crash after ledger write")
    assert await account.rows() == before
    restarted = PostgresStateStore(account.sessions)
    assert [r.key for r in await restarted.pending_outbox()] == [pending.key]
    assert await account.apply(pending)
    assert not await account.apply(pending)
    usage, ledger = await account.rows()
    assert usage[0].output_tokens == 120 and usage[0].observation_revision == 2
    assert sum((r.delta_usd for r in ledger), Decimal("0")) == Decimal("-0.001200")
    assert await restarted.pending_outbox() == []


async def test_record_and_apply_rolls_back_neutral_state_with_host_writes(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(session: AsyncSession, **kwargs: object) -> bool:
        raise RuntimeError("fixture process crash")

    with monkeypatch.context() as patch:
        patch.setattr(tenant_ledger, "insert_entry", crash)
        with pytest.raises(RuntimeError):
            await account.record(observation())
    assert await account.rows() == ([], [])
    assert await account.store.pending_outbox() == []
    async with account.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM usage_observation")) == 0
    assert await account.record(observation())


async def test_foreign_tenant_and_forged_outbox_fail_before_any_host_effect(
    account: Account,
) -> None:
    row = await account.queue(observation())
    async with account.sessions() as session, session.begin():
        with pytest.raises(ScopeViolation):
            await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=uuid.UUID(int=9),
                platform_user_id="user",
                pricing=RATES,
            )
    forged = row.model_copy(update={"observation": observation(output=200)})
    with pytest.raises(ValueError, match="durable outbox contents"):
        await account.apply(forged)
    assert await account.rows() == ([], [])
    assert [r.key for r in await account.store.pending_outbox()] == [row.key]


async def test_dm_exemption_does_not_write_even_neutral_state(account: Account) -> None:
    async with account.sessions() as session, session.begin():
        assert not await accounting_outbox.record_observation_usage(
            session,
            binding_id="binding",
            observation=observation(),
            tenant_id=None,
            platform_user_id="user",
            pricing=RATES,
        )
    assert await account.rows() == ([], [])
    assert await account.store.pending_outbox() == []


@pytest.mark.parametrize(
    "grain,basis",
    [("turn", "cumulative"), ("session", "cumulative"), ("model_request", "cumulative")],
)
async def test_c12_aggregates_cannot_double_bill_model_requests(
    account: Account, grain: str, basis: str
) -> None:
    assert await account.record(observation())
    before = await account.rows()
    total = observation(event_id="total", output=999).model_copy(
        update={"grain": grain, "basis": basis, "covers": ("event",)}
    )
    assert await account.record(total)
    assert await account.rows() == before
    assert await account.store.pending_outbox() == []


@pytest.mark.parametrize("grain", ["model_request", "turn", "session"])
@pytest.mark.parametrize("basis", ["increment", "cumulative"])
async def test_selected_provider_grain_bills_each_revision_once(
    account: Account, grain: accounting_outbox.BillingGrain, basis: str
) -> None:
    for revision, output in enumerate((100, 120, 110), 1):
        usage = observation(revision, output).model_copy(update={"grain": grain, "basis": basis})
        assert await account.record(usage, billing_grain=grain)
        assert not await account.record(usage, billing_grain=grain)
    rows, ledger = await account.rows()
    assert len(rows) == 1 and rows[0].output_tokens == 110
    assert {row.idempotency_key: row.delta_usd for row in ledger} == {
        "turn:session:event": Decimal("-0.001000"),
        "adjust:binding:event:2": Decimal("-0.000200"),
        "adjust:binding:event:3": Decimal("0.000100"),
    }


async def test_uncovered_wrong_grain_stays_pending_until_caller_selects_it(
    account: Account,
) -> None:
    row = await account.queue(
        observation().model_copy(update={"grain": "turn", "basis": "cumulative"})
    )
    with pytest.raises(ValueError, match="configured billing grain"):
        await account.apply(row)
    assert await account.rows() == ([], [])
    assert [pending.key for pending in await account.store.pending_outbox()] == [row.key]
    assert await account.apply(row, billing_grain="turn")
    _, ledger = await account.rows()
    assert len(ledger) == 1 and ledger[0].delta_usd == Decimal("-0.001000")
    assert not await account.apply(row, billing_grain="turn")


async def test_aggregate_requires_durable_coverage_then_can_precede_leaf_debit(
    account: Account,
) -> None:
    aggregate = await account.queue(
        observation(event_id="aggregate", output=999).model_copy(
            update={"grain": "turn", "basis": "cumulative", "covers": ("leaf",)}
        )
    )
    with pytest.raises(ValueError, match="durable observations"):
        await account.apply(aggregate)
    assert await account.rows() == ([], [])
    assert [row.key for row in await account.store.pending_outbox()] == [aggregate.key]
    leaf = await account.queue(observation(event_id="leaf"))
    assert await account.apply(aggregate)
    assert await account.rows() == ([], [])
    assert await account.apply(leaf)
    rows, ledger = await account.rows()
    assert len(rows) == len(ledger) == 1
    assert ledger[0].idempotency_key == "turn:session:leaf"
    assert ledger[0].delta_usd == Decimal("-0.001000")
    assert await account.store.pending_outbox() == []


@pytest.mark.parametrize(
    "covered_grain,covered_covers", [("turn", ()), ("model_request", ("leaf",))]
)
async def test_aggregate_cannot_claim_mismatched_or_recursive_coverage(
    account: Account, covered_grain: str, covered_covers: tuple[str, ...]
) -> None:
    await account.queue(
        observation(event_id="covered").model_copy(
            update={"grain": covered_grain, "covers": covered_covers}
        )
    )
    row = await account.queue(
        observation(event_id="aggregate").model_copy(
            update={"grain": "session", "covers": ("covered",)}
        )
    )
    with pytest.raises(ValueError, match="durable observations"):
        await account.apply(row)
    assert await account.rows() == ([], [])
    assert row.key in [pending.key for pending in await account.store.pending_outbox()]


async def test_unknown_and_explicit_zero_are_distinct_in_durable_state(account: Account) -> None:
    unknown = observation(output=None, input_tokens=None, cached=None, written=None)
    assert await account.record(unknown)
    assert await account.rows() == ([], [])
    assert await account.record(observation(2, 0))
    usage, ledger = await account.rows()
    assert len(usage) == 1 and usage[0].output_tokens == 0 and usage[0].observation_revision == 2
    assert not ledger


async def test_higher_unchanged_revision_updates_projection_without_adjustment(
    account: Account,
) -> None:
    assert await account.record(observation())
    assert await account.record(observation(2))
    usage, ledger = await account.rows()
    assert usage[0].observation_revision == 2 and len(ledger) == 1
    assert ledger[0].idempotency_key == "turn:session:event"


async def test_revision_without_prior_uses_adjust_key(account: Account) -> None:
    assert await account.record(observation(3))
    _, ledger = await account.rows()
    assert len(ledger) == 1 and ledger[0].idempotency_key == "adjust:binding:event:3"
    assert ledger[0].delta_usd == Decimal("-0.001000")


async def test_historical_baseline_mismatch_is_refused(account: Account) -> None:
    await record_turn_usage(
        sessionmaker=account.sessions,
        tenant_id=TENANT,
        platform_user_id="user",
        observation=observation(),
        pricing=RATES,
        channel_id="channel",
    )
    before = await account.rows()
    row = await account.queue(observation(output=200))
    with pytest.raises(ValueError, match="unchanged first revision"):
        await account.apply(row)
    assert await account.rows() == before
    assert [r.key for r in await account.store.pending_outbox()] == [row.key]


async def test_applier_requires_a_caller_transaction(account: Account) -> None:
    row = await account.queue(observation())
    async with account.sessions() as session:
        with pytest.raises(ValueError, match="caller-owned transaction"):
            await accounting_outbox.apply_usage_outbox(
                session,
                row,
                tenant_id=TENANT,
                platform_user_id="user",
                pricing=RATES,
            )
    assert await account.rows() == ([], [])


async def test_legacy_channel_spend_excludes_positive_non_spend_credits(account: Account) -> None:
    async with account.sessions() as session, session.begin():
        for key, amount, reason in (
            ("turn", Decimal("-1.250000"), "turn_debit"),
            ("checkpoint", Decimal("-0.250000"), "checkpoint_debit"),
            ("topup", Decimal("10"), "topup"),
            ("refund", Decimal("3"), "refund"),
            ("media", Decimal("-0.100000"), "media_debit"),
        ):
            await tenant_ledger.insert_entry(
                session,
                tenant_id=TENANT,
                idempotency_key=key,
                delta_usd=amount,
                reason=reason,
                channel_id="channel",
                occurred_at=NOW,
            )
        old_query = text(
            "SELECT -SUM(delta_usd) FROM tenant_ledger WHERE tenant_id=:tenant AND channel_id='channel' AND delta_usd < 0"
        )
        assert await session.scalar(old_query, {"tenant": TENANT}) == Decimal("1.600000")
        assert await tenant_ledger.get_channel_spend(
            session,
            tenant_id=TENANT,
            channel_id="channel",
            since=NOW,
            until=NOW + timedelta(days=1),
        ) == Decimal("1.600000")


@pytest.mark.parametrize("reason", ["turn_debit", "checkpoint_debit"])
async def test_current_legacy_turn_writers_only_write_negative_spend(
    account: Account, reason: TurnLedgerReason
) -> None:
    await record_turn_usage(
        sessionmaker=account.sessions,
        tenant_id=TENANT,
        platform_user_id="user",
        observation=observation(),
        pricing=RATES,
        markup=Decimal("1.25"),
        reason=reason,
        channel_id="channel",
    )
    _, ledger = await account.rows()
    assert len(ledger) == 1 and ledger[0].reason == reason
    assert ledger[0].delta_usd == Decimal("-0.001250")
    async with account.sessions() as session:
        original = await session.scalar(
            text(
                "SELECT -SUM(delta_usd) FROM tenant_ledger WHERE tenant_id=:tenant AND delta_usd < 0"
            ),
            {"tenant": TENANT},
        )
        assert (
            await tenant_ledger.get_channel_spend(
                session,
                tenant_id=TENANT,
                channel_id="channel",
                since=None,
                until=None,
            )
            == original
            == Decimal("0.001250")
        )


type Snapshot = tuple[tuple[tuple[object, ...], ...], tuple[tuple[str, Decimal], ...]]


async def _snapshot(account: Account, events: tuple[str, ...]) -> Snapshot:
    usage, ledger = await account.rows()
    return (
        tuple(
            (
                row.managed_session_id,
                row.event_id,
                row.model,
                row.input_tokens,
                row.output_tokens,
                row.cache_creation_input_tokens,
                row.cache_read_input_tokens,
                row.platform_user_id,
                row.channel_id,
                row.id,
                row.occurred_at,
                row.tenant_id,
            )
            for row in sorted(usage, key=lambda row: row.event_id)
            if row.event_id in events
        ),
        tuple(
            (row.idempotency_key, row.delta_usd)
            for row in sorted(ledger, key=lambda row: row.idempotency_key)
            if any(
                row.idempotency_key.startswith(
                    (f"turn:session:{event}", f"adjust:binding:{event}:")
                )
                for event in events
            )
        ),
    )


async def test_c12_host_hook_validates_real_database_facts_and_rejects_broken_evidence(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def host_evidence() -> Mapping[str, object]:
        await record_turn_usage(
            sessionmaker=account.sessions,
            tenant_id=TENANT,
            platform_user_id="user",
            observation=observation(event_id="history"),
            pricing=RATES,
            channel_id="channel",
        )
        history_before = await _snapshot(account, ("history",))
        assert await account.record(observation(event_id="history"))
        assert not await account.record(observation(event_id="history"))
        history_after = await _snapshot(account, ("history",))
        for revision, count in enumerate((100, 120, 110), 1):
            assert await account.record(observation(revision, count, event_id="correction"))
        corrections = await _snapshot(account, ("correction",))
        async with account.sessions() as session:
            spend = await tenant_ledger.get_channel_spend(
                session,
                tenant_id=TENANT,
                channel_id="channel",
                since=None,
                until=None,
            )
        overlap_before = await _snapshot(account, ("history", "correction"))
        for grain in ("turn", "session"):
            assert await account.record(
                observation(event_id=f"total-{grain}", output=999).model_copy(
                    update={"grain": grain, "basis": "cumulative", "covers": ("correction",)}
                )
            )
        overlap_after = await _snapshot(account, ("history", "correction"))
        async with account.sessions() as session:
            bodies = (
                (
                    await session.execute(
                        text(
                            "SELECT applied->'observation' FROM usage_observation WHERE observation_id LIKE 'total-%' ORDER BY applied->'observation'->>'grain'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert await account.record(observation(event_id="crash"))
        crash_before = await _snapshot(account, ("crash",))
        pending = await account.queue(observation(2, 120, event_id="crash"))
        pending_before = len(await account.store.pending_outbox())
        projection_at_failure: int | None = None

        async def crash_after_usage(session: AsyncSession, **kwargs: object) -> bool:
            nonlocal projection_at_failure
            rows = await usage_events.list_for_tenant(session, tenant_id=TENANT)
            projection_at_failure = next(
                row.output_tokens for row in rows if row.event_id == "crash"
            )
            raise RuntimeError("fixture process crash after projection")

        with monkeypatch.context() as patch:
            patch.setattr(tenant_ledger, "insert_entry", crash_after_usage)
            with pytest.raises(RuntimeError):
                await account.apply(pending)
        after_failure = await _snapshot(account, ("crash",))
        restarted = PostgresStateStore(account.sessions)
        pending_after = len(await restarted.pending_outbox())
        assert await account.apply(pending)
        assert not await account.apply(pending)
        captured.update(
            {
                "binding_id": "binding",
                "history_before": history_before,
                "history_after": history_after,
                "corrections": corrections,
                "correction_channel_spend": spend
                - sum((-r[1] for r in history_after[1]), Decimal("0")),
                "overlap_before": overlap_before,
                "overlap_after": overlap_after,
                "aggregate_grains": tuple(body["grain"] for body in bodies),
                "aggregate_covers": tuple(tuple(body["covers"]) for body in bodies),
                "crash_before": crash_before,
                "crash_after_failure": after_failure,
                "projection_at_failure": projection_at_failure,
                "pending_before_failure": pending_before,
                "pending_after_failure": pending_after,
                "crash_after_restart": await _snapshot(account, ("crash",)),
                "pending_after_restart": len(await restarted.pending_outbox()),
            }
        )
        return captured

    adapter = create()
    # This callable returns queried effects; the shared fixture makes the verdict.
    monkeypatch.setattr(adapter.transport, "host_accounting_evidence", host_evidence, raising=False)
    result = await c12(adapter.driver, account.store, adapter.transport)
    assert result.status == "pass" and result.evidence
    corrections = cast(Snapshot, captured["corrections"])
    for changed in (
        {"history_after": ((), ())},
        {"corrections": (corrections[0], ())},
        {"correction_channel_spend": Decimal("0.001200")},
        {"overlap_after": ((), ())},
        {"crash_after_failure": ((), ())},
        {"pending_after_failure": 0},
        {"pending_after_restart": 1},
    ):

        async def broken(bad: Mapping[str, object] = changed) -> Mapping[str, object]:
            return captured | bad

        monkeypatch.setattr(adapter.transport, "host_accounting_evidence", broken)
        with pytest.raises(ConformanceFailure, match="C12:"):
            await c12(adapter.driver, account.store, adapter.transport)


async def test_c12_missing_or_empty_host_evidence_cannot_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create()
    assert (await c12(adapter.driver, adapter.store, adapter.transport)).status == "pending"

    async def empty() -> Mapping[str, object]:
        return {}

    monkeypatch.setattr(adapter.transport, "host_accounting_evidence", empty, raising=False)
    with pytest.raises(ConformanceFailure, match="C12:"):
        await c12(adapter.driver, adapter.store, adapter.transport)


@pytest.mark.parametrize("reason", ["turn_debit", "checkpoint_debit"])
async def test_negative_markup_legacy_credit_is_still_excluded_from_channel_spend(
    account: Account, reason: TurnLedgerReason
) -> None:
    await record_turn_usage(
        sessionmaker=account.sessions,
        tenant_id=TENANT,
        platform_user_id="user",
        observation=observation(),
        pricing=RATES,
        markup=Decimal("-1"),
        reason=reason,
        channel_id="channel",
    )
    _, ledger = await account.rows()
    assert len(ledger) == 1 and ledger[0].delta_usd == Decimal("0.001000")
    assert ledger[0].idempotency_key == "turn:session:event"
    async with account.sessions() as session:
        old = await session.scalar(
            text(
                "SELECT COALESCE(-SUM(delta_usd),0) FROM tenant_ledger WHERE tenant_id=:tenant AND channel_id='channel' AND delta_usd < 0"
            ),
            {"tenant": TENANT},
        )
        assert (
            await tenant_ledger.get_channel_spend(
                session,
                tenant_id=TENANT,
                channel_id="channel",
                since=None,
                until=None,
            )
            == old
            == Decimal("0.000000")
        )

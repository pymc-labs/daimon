"""Native SDK failure -> retained scoped usage -> real accounting, offline only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.errors import TurnError
from daimon.core.mux_backend import TurnBackendRequest
from daimon.core.pricing import provider_cost_of
from daimon.core.stores import tenant_ledger, turn_outcomes, usage_events
from daimon.core.turn import driver
from daimon.core.turn.gemini import PROFILE, GeminiTurnIO, GeminiUsageRuntime, gemini_backend
from daimon.core.turn.io import TurnConnectionLost, TurnIO
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.run import prepared_billing
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_billing import ObservationBilled
from daimon.testing.conformance.budget import (
    ActualSpend,
    BudgetConfig,
    BudgetGuard,
    BudgetRefused,
    MeasuredRequest,
    ProbePlan,
    TokenLimits,
    TokenUsage,
)
from daimon.testing.drivers.gemini.live_cert import ReceiptGuard
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Page, PageRequest, Provider, ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.storage import Records
from mux.errors import ProviderError, ProviderErrorCategory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from .test_gemini_preparation import CONFIG, TENANT
from .test_gemini_termination import (
    SCOPE,
    NativeHost,
    prepare,
    run,
)
from .test_gemini_termination import (
    clean_sdk_settings as clean_sdk_settings,
)
from .test_gemini_termination import (
    native_host as native_host,
)
from .test_gemini_termination import (
    owned_database_preflight as owned_database_preflight,
)


def refuse_meter(native: NativeHost, *, unknown: bool = False) -> None:
    native.wire.status = "in_progress"
    native.wire.get_status = 429
    native.wire.repeat_get_error = True
    native.wire.retry_after_s = 12
    if unknown:
        native.wire.usage = {}


async def codec(native: NativeHost) -> tuple[GeminiTurnIO, ObservationBilled]:
    prepared = await prepare(native)
    assert prepared.session_ref is not None
    backend = gemini_backend(
        CONFIG,
        SCOPE,
        native.deps.turn_runtimes[PROFILE],
        prepared.session_ref.account_scope_id,
    )
    billing = prepared_billing(prepared, tenant_id=TENANT)
    assert isinstance(billing, ObservationBilled)
    return GeminiTurnIO(backend, SCOPE, prepared.session_ref), billing


async def balance(native: NativeHost) -> Decimal:
    async with native.deps.sessionmaker() as db:
        return await tenant_ledger.get_balance(db, tenant_id=TENANT)


def guard(root: Path) -> tuple[ReceiptGuard, ProbePlan]:
    config, ledger = root / "budget.json", root / "spend.md"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "total_cap_usd": "150",
                "ledger_path": str(ledger),
                "providers": {
                    "gemini": {
                        "cap_usd": "30",
                        "models": {
                            CONFIG.model: {
                                "input": "0.75",
                                "cached_input": "0.075",
                                "cache_write_input": "0.75",
                                "output": "3.75",
                                "effective_from": "2026-10-10",
                                "source": "https://ai.google.dev/gemini-api/docs/pricing",
                            }
                        },
                    }
                },
            }
        )
    )
    BudgetGuard.initialize(config, ledger)
    return ReceiptGuard(BudgetGuard(config, ledger)), ProbePlan(
        provider="gemini",
        model=FLASH_PRIMARY,
        fixture_id="C18",
        limits=TokenLimits(input_tokens=1000, output_tokens=1000),
    )


def actual(usage: UsageObservation, *, terminal: bool) -> ActualSpend:
    return ActualSpend(
        requests=(
            MeasuredRequest(
                id=usage.id,
                observed_at=usage.observed_at,
                tokens=TokenUsage(
                    input_tokens=usage.input_tokens,
                    input_cached_tokens=usage.input_cached_tokens,
                    input_cache_write_tokens=0,
                    output_tokens=usage.output_tokens,
                ),
            ),
        ),
        containers=(),
        usage_complete=terminal,
    )


@pytest.mark.parametrize("wrapper", ["prepared", "driver"])
async def test_public_repeated_429_keeps_original_failure_and_durable_unknown(
    native_host: NativeHost, wrapper: str, tmp_path: Path
) -> None:
    refuse_meter(native_host, unknown=True)
    lifecycle = RecordingLifecycle()
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    if wrapper == "prepared":
        outcome = await run(native_host, lifecycle=lifecycle)
        state = outcome.state
        assert outcome.ma_session_id == prepared.ma_session_id and not outcome.recovered
    else:
        io, billing = await codec(native_host)
        state = await driver.run_turn(
            anthropic=native_host.deps.anthropic,
            session_id=io.session.id,
            user_message="offline question",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=billing,
            path="mux",
            backend=io.backend,
            scope=SCOPE,
            session_ref=io.session,
            profile=PROFILE,
            backend_request=TurnBackendRequest(
                profile=PROFILE,
                client=native_host.deps.anthropic,
                scope=SCOPE,
                session_id=io.session.id,
                session=io.session,
                config=CONFIG,
            ),
        )
    assert state.termination == TerminationReason.RATE_LIMITED
    assert isinstance(state.error, TurnError)
    assert isinstance(state.error.cause, ProviderError)
    assert state.error.cause.category == "rate_limited"
    assert len(lifecycle.terminal_failures) == 1
    final, error = lifecycle.terminal_failures[0]
    assert state.error is error is final.error
    assert state.rate_limit_until == final.rate_limit_until
    # The SDK currently exposes no retry-after metadata on this error. Keep
    # the original nullable timing unchanged rather than synthesizing a date.
    assert state.rate_limit_until is None and not lifecycle.rate_limits
    assert not lifecycle.terminal_success and not lifecycle.reconnects
    assert sum(r.method == "POST" for r in native_host.wire.requests) == 1
    assert sum(r.method == "GET" for r in native_host.wire.requests) == 2
    assert not native_host.ancillary.requests
    pending = await native_host.journal.state_store.pending_outbox()
    assert len(pending) == 1
    usage = pending[0].observation
    assert usage.session == prepared.session_ref and usage.turn_id
    assert usage.completeness == "unknown"
    assert usage.input_tokens is usage.output_tokens is usage.input_cached_tokens is None
    assert await balance(native_host) == 0
    async with native_host.deps.sessionmaker() as db:
        assert not await usage_events.list_for_tenant(db, tenant_id=TENANT)
        assert await db.scalar(text("SELECT count(*) FROM tenant_ledger")) == 0
        bodies = (
            (await db.execute(text("SELECT applied->'observation' FROM usage_observation")))
            .scalars()
            .all()
        )
        assert len(bodies) == 1 and bodies[0]["completeness"] == "unknown"
        assert bodies[0]["input_tokens"] is bodies[0]["output_tokens"] is None
    if wrapper == "prepared":
        await drain_outcomes()
        async with native_host.deps.sessionmaker() as db:
            rows = await turn_outcomes.list_for_tenant(db, TENANT)
        rate_limited = [row for row in rows if row.reason == TerminationReason.RATE_LIMITED]
        assert len(rate_limited) == 1
        row = rate_limited[0]
        assert row.session_id == prepared.ma_session_id
        assert row.account_id == prepared.admission.account_id and not row.recovered
        assert row.error_class == "TurnError" and row.cost_usd is None
        assert row.input_tokens is row.output_tokens is None
    # Existing public guard: a failed host reason never implies a complete meter.
    observed, plan = guard(tmp_path)
    held = observed.reserve(plan)
    receipt = observed.settle(
        held, status="failed", limits=plan.limits, actual=actual(usage, terminal=False)
    )
    assert receipt.actual_usd is None and receipt.held_usd == held.receipt.reserved_usd > 0
    assert receipt.accounting_status == "estimated_unverified"
    restarted = BudgetGuard(observed.config_path, observed.spend_path)
    assert restarted.report() == (receipt,)


async def test_single_429_followed_by_measured_get_charges_once(native_host: NativeHost) -> None:
    refuse_meter(native_host)
    native_host.wire.repeat_get_error = False
    native_host.wire.final_get_status = "failed"
    lifecycle = RecordingLifecycle()
    outcome = await run(native_host, lifecycle=lifecycle)
    assert outcome.termination == TerminationReason.RATE_LIMITED
    assert isinstance(outcome.state.error, TurnError)
    assert len(lifecycle.terminal_failures) == 1 and not lifecycle.reconnects
    assert await balance(native_host) == Decimal("-0.000078")
    assert not await native_host.journal.state_store.pending_outbox()
    assert native_host.wire.saved["offline-interaction-1"]["status"] == "failed"
    async with native_host.deps.sessionmaker() as db:
        rows = await usage_events.list_for_tenant(db, tenant_id=TENANT)
        assert len(rows) == 1
        assert await db.scalar(text("SELECT count(*) FROM tenant_ledger")) == 1


async def test_known_charge_unknown_final_retains_charge_then_native_revision_settles_delta(
    native_host: NativeHost, tmp_path: Path
) -> None:
    io, billing = await codec(native_host)
    native_host.wire.status = "in_progress"
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline question"}]}]
    )
    initial = TurnState()
    await driver._replay_usage(io, billing, [initial], set())  # pyright: ignore[reportPrivateUsage]
    assert await balance(native_host) == Decimal("-0.000078")
    raw = native_host.wire.saved["offline-interaction-1"]
    raw["updated"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    raw["usage"] = {}
    unknown = (await io.replay_usage())[0]
    assert unknown.revision == 2 and unknown.completeness == "unknown"
    original = TurnError(kind="upstream", message="original terminal failure")
    final = TurnState(termination=TerminationReason.RATE_LIMITED, error=original)
    cell = [final]
    native_host.wire.get_status = 429
    native_host.wire.repeat_get_error = True
    await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
        io, billing, cell, set(), finalized_rate_limit=True
    )
    assert cell[0].error is original and cell[0].termination == TerminationReason.RATE_LIMITED
    assert await balance(native_host) == Decimal("-0.000078")
    pending = await native_host.journal.state_store.pending_outbox()
    assert len(pending) == 1 and pending[0].observation == unknown
    observed, plan = guard(tmp_path)
    held = observed.reserve(plan)
    receipt = observed.settle(
        held, status="failed", limits=plan.limits, actual=actual(unknown, terminal=False)
    )
    assert receipt.actual_usd is None and receipt.held_usd == held.receipt.reserved_usd
    # A later authoritative SDK GET supplies a genuine higher revision of the
    # same native interaction. The existing recorder/outbox settles only delta.
    native_host.wire.get_status = None
    raw["updated"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    raw["status"] = "failed"
    raw["usage"] = {
        "total_input_tokens": 64,
        "total_cached_tokens": 16,
        "total_output_tokens": 16,
        "total_thought_tokens": 3,
    }
    verified = (await io.replay_usage())[0]
    assert verified.id == unknown.id and verified.turn_id == unknown.turn_id
    assert verified.session == unknown.session and verified.revision == 3
    assert verified.completeness == "measured"
    usage_runtime = native_host.deps.turn_runtimes[PROFILE].usage_store
    assert isinstance(usage_runtime, GeminiUsageRuntime)
    exact = provider_cost_of(
        verified, usage_runtime.prices[FLASH_PRIMARY], infrastructure_usd=Decimal(0)
    )
    assert exact is not None
    assert exact == Decimal("0.00010845")
    await driver._replay_usage(io, billing, cell, set())  # pyright: ignore[reportPrivateUsage]
    await driver._replay_usage(io, billing, cell, set())  # pyright: ignore[reportPrivateUsage]
    assert await balance(native_host) == Decimal("-0.000108")
    async with native_host.deps.sessionmaker() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT delta_usd, idempotency_key FROM tenant_ledger ORDER BY occurred_at, idempotency_key"
                )
            )
        ).all()
    assert sorted(row[0] for row in rows) == [Decimal("-0.000078"), Decimal("-0.000030")]
    assert len({row[1] for row in rows}) == 2
    assert any(row[1].startswith("adjust:") and row[1].endswith(":3") for row in rows)
    # Hold release is the unchanged operator-reviewed public reconciliation
    # path, not a new driver policy or an automatic release on a failed turn.
    proposal = observed.propose_reconciliation(
        held.receipt.run_id,
        evidence_sha256=hashlib.sha256(verified.model_dump_json().encode()).hexdigest(),
        basis="provider_usage",
        token_usd=exact,
        container_usd=Decimal(0),
        price_effective_from=verified.observed_at.date(),
    )
    with pytest.raises(BudgetRefused, match="lead-signed"):
        observed.reconcile(proposal)
    config = BudgetConfig.load(observed.config_path)
    observed.config_path.write_text(
        config.model_copy(
            update={"approved_reconciliations": frozenset({proposal.digest})}
        ).model_dump_json()
    )
    settled = observed.reconcile(proposal)
    assert settled.actual_usd == Decimal("0.00010845") and settled.held_usd == 0
    assert cell[0].error is original and cell[0].termination == TerminationReason.RATE_LIMITED


@pytest.mark.parametrize(
    "failure",
    [
        "wrong-root",
        "missing-root",
        "missing-observation",
        "wrong-identity",
        "local-read",
        "foreign-session",
        "foreign-model",
        "missing-model",
        "wrong-revision",
    ],
)
async def test_retained_evidence_failures_propagate(
    native_host: NativeHost, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    io, billing = await codec(native_host)
    native_host.wire.status = "in_progress"
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline question"}]}]
    )
    if failure == "missing-root":
        io.root = None
    original_list = io.backend.usage.list

    async def bad_list(
        scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        if failure == "local-read":
            raise ProviderError("rate_limited", retryable=True, native_code="local_storage_failure")
        result = await original_list(scope, session, page=page)
        if failure == "missing-observation":
            return result.model_copy(update={"data": ()})
        if failure == "wrong-revision":
            return result.model_copy(
                update={
                    "data": tuple(
                        value.model_copy(update={"revision": value.revision + 1})
                        for value in result.data
                    )
                }
            )
        if failure in {"wrong-root", "wrong-identity"}:
            changes = (
                {"turn_id": "wrong-root"} if failure == "wrong-root" else {"id": "wrong-identity"}
            )
            return result.model_copy(
                update={"data": tuple(value.model_copy(update=changes) for value in result.data)}
            )
        if failure in {"foreign-session", "foreign-model", "missing-model"}:
            value = result.data[0]
            assert value.model is not None
            changed: dict[str, object] = (
                {"session": value.session.model_copy(update={"account_id": "other-account"})}
                if failure == "foreign-session"
                else {"model": None}
                if failure == "missing-model"
                else {"model": value.model.model_copy(update={"provider": "openai"})}
            )
            return result.model_copy(update={"data": (value.model_copy(update=changed),)})
        return result

    monkeypatch.setattr(io.backend.usage, "list", bad_list)
    refuse_meter(native_host)
    with pytest.raises(ProviderError) as caught:
        await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
            io, billing, [TurnState()], set(), finalized_rate_limit=True
        )
    assert caught.value.native_code != "429"
    assert await balance(native_host) == 0
    assert not await native_host.journal.state_store.pending_outbox()


async def test_real_postgres_recorder_failure_is_not_swallowed(
    native_host: NativeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tenant_ledger.insert_entry

    async def fail_write(db: AsyncSession, **kwargs: Any) -> bool:
        # Actual DB syntax failure in the billing transaction, after the
        # original insertion; both writes must roll back and the error escape.
        result = await original(db, **kwargs)
        await db.execute(text("SELECT nonexistent_terminal_usage_proof_column"))
        return result

    monkeypatch.setattr(tenant_ledger, "insert_entry", fail_write)
    refuse_meter(native_host)
    lifecycle = RecordingLifecycle()
    with pytest.raises(DBAPIError):
        await run(native_host, lifecycle=lifecycle)
    assert not lifecycle.terminal_success
    assert lifecycle.renders[-1].termination == TerminationReason.RATE_LIMITED
    assert await balance(native_host) == 0
    async with native_host.deps.sessionmaker() as db:
        assert not await usage_events.list_for_tenant(db, tenant_id=TENANT)


async def test_retained_storage_transaction_failure_is_not_swallowed(
    native_host: NativeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    io, billing = await codec(native_host)
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline question"}]}]
    )
    original = native_host.journal.storage.transaction
    reads = 0

    @asynccontextmanager
    async def broken_read() -> AsyncIterator[Records]:
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("offline retained storage unavailable")
        async with original() as records:
            yield records

    monkeypatch.setattr(native_host.journal.storage, "transaction", broken_read)
    refuse_meter(native_host)
    with pytest.raises(OSError, match="retained storage unavailable"):
        await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
            io, billing, [TurnState()], set(), finalized_rate_limit=True
        )
    assert reads == 2 and await balance(native_host) == 0


@pytest.mark.parametrize(
    "category,retryable",
    [
        ("upstream", False),
        ("rate_limited", False),
        ("overloaded", True),
        ("transient_network", True),
    ],
)
async def test_other_fetch_errors_propagate(
    native_host: NativeHost,
    monkeypatch: pytest.MonkeyPatch,
    category: ProviderErrorCategory,
    retryable: bool,
) -> None:
    io, _ = await codec(native_host)
    error = ProviderError(category, retryable=retryable)

    async def fail(scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        raise error

    monkeypatch.setattr(io.backend.usage, "reconcile", fail)
    with pytest.raises(ProviderError) as caught:
        await io.replay_usage_after_rate_limit()
    assert caught.value is error


async def test_success_and_reconnect_reconciliation_still_raise(native_host: NativeHost) -> None:
    io, billing = await codec(native_host)
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline question"}]}]
    )
    refuse_meter(native_host)
    for state in (TurnState(), TurnState(termination=TerminationReason.COMPLETED)):
        with pytest.raises(ProviderError) as caught:
            await driver._replay_usage(io, billing, [state], set())  # pyright: ignore[reportPrivateUsage]
        assert caught.value.category == "rate_limited"
    with pytest.raises(ProviderError):
        await io.replay_usage()
    assert await balance(native_host) == 0


@pytest.mark.parametrize(
    "provider,hook", [("anthropic", True), ("openai", True), ("gemini", False)]
)
async def test_missing_hook_and_other_providers_retain_error(
    provider: Provider, hook: bool
) -> None:
    error = ProviderError("rate_limited", retryable=True)

    class IO:
        session = ResourceRef(
            id="session", kind="session", provider=provider, account_scope_id="scope"
        )

        replay_usage_after_rate_limit: object

        async def replay_usage(self) -> Sequence[UsageObservation]:
            raise error

    io = IO()
    if hook:

        async def forbidden() -> Sequence[UsageObservation]:
            raise AssertionError("foreign retained usage hook invoked")

        io.replay_usage_after_rate_limit = forbidden

    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("no observation may reach accounting")

    with pytest.raises(ProviderError) as caught:
        await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
            cast(TurnIO, io),
            ObservationBilled(record=record),
            [TurnState()],
            set(),
            finalized_rate_limit=True,
        )
    assert caught.value is error


@pytest.mark.parametrize("error", [TimeoutError(), asyncio.CancelledError()])
async def test_fetch_deadline_and_cancellation_propagate(
    native_host: NativeHost, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    io, _ = await codec(native_host)

    async def fail(scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        raise error

    monkeypatch.setattr(io.backend.usage, "reconcile", fail)
    with pytest.raises(type(error)) as caught:
        await io.replay_usage_after_rate_limit()
    assert caught.value is error


async def test_retained_reducer_failure_propagates_after_real_debit(
    native_host: NativeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid(state: TurnState, usage: UsageObservation) -> TurnState:
        raise ValueError("invalid retained usage aggregation")

    monkeypatch.setattr(driver, "apply_usage_observation", invalid)
    refuse_meter(native_host)
    lifecycle = RecordingLifecycle()
    with pytest.raises(ValueError, match="invalid retained usage aggregation"):
        await run(native_host, lifecycle=lifecycle)
    assert not lifecycle.terminal_success
    assert await balance(native_host) == Decimal("-0.000078")
    async with native_host.deps.sessionmaker() as db:
        assert await db.scalar(text("SELECT count(*) FROM tenant_ledger")) == 1


async def test_failed_fetch_diagnostic_is_closed_and_keeps_original_cause(
    native_host: NativeHost,
) -> None:
    refuse_meter(native_host, unknown=True)
    lifecycle = RecordingLifecycle()
    with capture_logs() as logs:
        result = await run(native_host, lifecycle=lifecycle)
    diagnostic = [row for row in logs if row["event"] == "turn.usage_reconciliation_failed"]
    assert diagnostic == [
        {
            "event": "turn.usage_reconciliation_failed",
            "category": "rate_limited",
            "error_class": "ProviderError",
            "log_level": "warning",
        }
    ]
    assert result.state.error is lifecycle.terminal_failures[0][1]
    assert result.termination == TerminationReason.RATE_LIMITED


async def test_measured_retained_snapshot_is_not_final_usage_and_keeps_hold(
    native_host: NativeHost, tmp_path: Path
) -> None:
    io, _ = await codec(native_host)
    refuse_meter(native_host)
    result = await run(native_host)
    assert result.termination == TerminationReason.RATE_LIMITED
    retained = tuple((await io.usage_snapshots()).values())
    assert len(retained) == 1
    usage = retained[0]
    assert usage.completeness == "measured"
    assert await balance(native_host) == Decimal("-0.000078")
    assert not await native_host.journal.state_store.pending_outbox()
    observed, plan = guard(tmp_path)
    held = observed.reserve(plan)
    receipt = observed.settle(
        held, status="failed", limits=plan.limits, actual=actual(usage, terminal=False)
    )
    assert receipt.actual_usd is None
    assert receipt.held_usd == held.receipt.reserved_usd > 0
    assert receipt.actual_evidence is not None and not receipt.actual_evidence.usage_complete
    assert usage == tuple((await io.usage_snapshots()).values())[0]


async def test_recorder_rate_limit_is_not_a_fetch_error(
    native_host: NativeHost,
) -> None:
    io, _ = await codec(native_host)
    await io.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline question"}]}]
    )
    refuse_meter(native_host)
    error = ProviderError("rate_limited", retryable=True, native_code="recorder_failure")

    async def record(*, observation: UsageObservation) -> bool:
        raise error

    with pytest.raises(ProviderError) as caught:
        await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
            io, ObservationBilled(record=record), [TurnState()], set(), finalized_rate_limit=True
        )
    assert caught.value is error
    assert await balance(native_host) == 0


async def test_ordinary_replay_does_not_inspect_the_optional_failure_hook() -> None:
    error = ProviderError("rate_limited", retryable=True)

    class IO:
        @property
        def session(self) -> ResourceRef:
            raise AssertionError("ordinary replay inspected the failure-only provider edge")

        async def replay_usage(self) -> Sequence[UsageObservation]:
            raise error

    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("no observations expected")

    with pytest.raises(ProviderError) as caught:
        await driver._replay_usage(  # pyright: ignore[reportPrivateUsage]
            cast(TurnIO, IO()), ObservationBilled(record=record), [TurnState()], set()
        )
    assert caught.value is error


@pytest.mark.parametrize("wrapper", ["prepared", "driver"])
@pytest.mark.parametrize("reconnect", [False, True], ids=["completed", "reconnected"])
async def test_completed_final_fetch_refuses_failure_only_usage_policy(
    native_host: NativeHost,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    reconnect: bool,
) -> None:
    native_host.wire.status = "completed"
    before_fetch: list[tuple[Decimal, int]] = []
    original_send = GeminiTurnIO.send
    sends = 0

    async def accepted_then_disconnect(
        io: GeminiTurnIO, events: Sequence[BetaManagedAgentsEventParams]
    ) -> None:
        nonlocal sends
        await original_send(io, events)
        sends += 1
        if sends == 1:
            # Actual native POST and acceptance journal precede this fault.
            raise TurnConnectionLost("offline connection lost after accepted POST")

    async def forbid_retained_policy(io: GeminiTurnIO) -> Sequence[UsageObservation]:
        raise AssertionError("completed turn used rate-limit-only retained usage")

    monkeypatch.setattr(GeminiTurnIO, "replay_usage_after_rate_limit", forbid_retained_policy)
    if reconnect:
        monkeypatch.setattr(GeminiTurnIO, "send", accepted_then_disconnect)

    class FinalFetchFailure(RecordingLifecycle):
        async def on_terminal_success(self, state: TurnState) -> None:
            await super().on_terminal_success(state)
            assert state.termination == TerminationReason.COMPLETED and state.error is None
            async with native_host.deps.sessionmaker() as db:
                count = await db.scalar(text("SELECT count(*) FROM tenant_ledger"))
            assert isinstance(count, int)
            before_fetch.append((await balance(native_host), count))
            # Fail only the post-terminal native meter fetch. Reconnect's
            # earlier SDK replay and any corresponding debit stay intact.
            native_host.wire.get_status = 429
            native_host.wire.repeat_get_error = True

    lifecycle = FinalFetchFailure()
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    with pytest.raises(ProviderError) as caught:
        if wrapper == "prepared":
            await run(native_host, lifecycle=lifecycle)
        else:
            io, billing = await codec(native_host)
            await driver.run_turn(
                anthropic=native_host.deps.anthropic,
                session_id=io.session.id,
                user_message="offline question",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=billing,
                path="mux",
                backend=io.backend,
                scope=SCOPE,
                session_ref=io.session,
                profile=PROFILE,
                backend_request=TurnBackendRequest(
                    profile=PROFILE,
                    client=native_host.deps.anthropic,
                    scope=SCOPE,
                    session_id=io.session.id,
                    session=io.session,
                    config=CONFIG,
                ),
            )
    assert caught.value.category == "rate_limited" and caught.value.retryable
    assert len(before_fetch) == len(lifecycle.terminal_success) == 1
    assert not lifecycle.terminal_failures
    assert bool(lifecycle.reconnects) is reconnect
    assert sends == int(reconnect)
    assert sum(r.method == "POST" for r in native_host.wire.requests) == 1
    assert sum(r.method == "GET" for r in native_host.wire.requests) >= 1
    assert not native_host.ancillary.requests
    assert await balance(native_host) == before_fetch[0][0]
    async with native_host.deps.sessionmaker() as db:
        assert await db.scalar(text("SELECT count(*) FROM tenant_ledger")) == before_fetch[0][1]
    # Genuine live usage was already billed; final-fetch failure neither
    # erases that debit nor bills a retained snapshot again.
    assert before_fetch == [(Decimal("-0.000078"), 1)]

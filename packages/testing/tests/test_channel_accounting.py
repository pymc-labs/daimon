"""Real host driver/recorders + owned Postgres; offline prices/transport only."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import channel_budgets, tenant_ledger
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn import driver
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.io import TurnEvent
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import PreparedTurn
from daimon.testing import channel_accounting
from daimon.testing.channel_accounting import ChannelAccounting, ControlledTurnPricing
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_channel_budget, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from daimon.testing.turn_router import build_turn_router, turn_events
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.gemini.usage import observation_from_interaction
from mux.drivers.openai.usage import observation as openai_observation
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from pydantic import TypeAdapter
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TENANT = uuid.UUID(int=8150)
ACCOUNT = uuid.UUID(int=8151)
SCOPE = Scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), principal_id="member", authorization_id="owned"
)
CHANNEL = ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="budget-channel")


class NativeStream:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.frames)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


class OfflineNativePort:
    """Mock ONLY the TurnIO port, retaining real host driver and native meters.

    Display frames contain no span/model meter. Both native normalizers remain
    real. This is accounting proof, not OpenAI/Gemini codec certification.
    """

    def __init__(self, usage):
        self.usage = usage
        self.sends = []
        self.display = TypeAdapter(list[BetaManagedAgentsSessionEvent]).validate_python(
            turn_events(agent_text="TINY-OK", usage_event_id=None)
        )
        self.streams = []

    async def send(self, events):
        self.sends.append(events)

    async def status(self):
        return "idle"

    async def open_stream(self, **kwargs):
        frames = [TurnEvent(native=self.display[0])]
        if self.usage is not None:
            frames.append(TurnEvent(usage=self.usage))
        frames.append(TurnEvent(native=self.display[-1]))
        stream = NativeStream(frames)
        self.streams.append(stream)
        return stream

    async def replay(self, **kwargs):
        return self.display

    async def replay_usage(self):
        return (self.usage,) if self.usage is not None else ()

    async def interrupt(self, **kwargs):
        return None

    async def archive(self):
        pass


@dataclass
class Context:
    reader: ChannelAccounting
    deps: TurnDeps
    prepared: PreparedTurn
    pricing: ControlledTurnPricing
    binding: ProviderBinding | None
    provider: str
    port: OfflineNativePort | None

    async def run(self, **changes):
        args = dict(
            deps=self.deps,
            prepared=self.prepared,
            pricing=self.pricing,
            binding=self.binding,
            thread_id="thread",
            external_user_id="member",
            user_message="QA budget 1: reply with only TINY-OK. No tools.",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            operation_key="owned-budget-turn",
        )
        args.update(changes)
        return await self.reader.run_turn(**args)


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def context(request, db_preflight, db_session_factory, db_clean, monkeypatch):
    require_owned_sessionmaker(db_session_factory)
    provider = request.param
    model = {
        "anthropic": "claude-haiku-4-5",
        "openai": "gpt-6-luna",
        "gemini": "gemini-3.8-flash",
    }[provider]
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, id=TENANT)
        await make_channel_budget(
            db,
            tenant=tenant,
            channel_id=CHANNEL.channel_id,
            limit_usd=Decimal("0.01"),
            window="total",
        )
    router = build_turn_router(
        str(TENANT), model_id=model, agent_text="TINY-OK", session_id="owned-session"
    )
    client = build_fake_anthropic(router.dispatch)
    deps = TurnDeps(
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="legacy" if provider == "anthropic" else "mux",
        state_store=PostgresStateStore(db_session_factory),
        tool_safety=OPEN_TOOL_SAFETY,
    )
    admission = Admission(
        account_id=ACCOUNT,
        agent=ma_agent(model=model, tenant_id=TENANT),
        environment=ma_environment(),
        config=ResolvedConfig(agent_name="test", environment_name="test"),
        budget_channel_id=CHANNEL.channel_id,
    )

    # A poisoned preparation recorder proves the seam selects PRODUCTION
    # recorders instead of trusting a caller's fake ledger-writing callback.
    async def forbidden_record(**kwargs):
        raise RuntimeError("preparation recorder must be replaced by actual billing")

    prepared = PreparedTurn(
        admission=admission,
        ma_session_id="owned-session",
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=forbidden_record,
    )
    binding, port = None, None
    if provider == "anthropic":
        pricing = ControlledTurnPricing(
            "mock-budget-rates-100-usd-per-million",
            model,
            legacy_rates=ModelRates(100, 100, 100, 100),
        )
    else:
        profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
        revision = ConfigRevision.create(
            CHANNEL,
            1,
            resolve_default(BackendConfig(backend=provider, profile=profile, model=model)),
        )
        ref = ResourceRef(
            id=prepared.ma_session_id,
            kind="session",
            provider=provider,
            account_scope_id="owned-workspace",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
        binding = ProviderBinding(
            id="owned-binding",
            thread=ThreadRef(channel=CHANNEL, thread_id="thread"),
            provider=provider,
            profile=profile,
            native_refs={"session": ref.id},
            generation=1,
            config_revision=revision.local,
            legacy_account_id=SCOPE.account_id,
        )
        await deps.state_store.put_binding(binding, expected_generation=0)
        prepared = replace(
            prepared,
            admission=replace(
                admission, backend_revision=revision, backend_revision_origin="channel"
            ),
            session_ref=ref,
        )
        if provider == "openai":
            usage = openai_observation(
                {
                    "id": "owned-root",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                },
                ref,
                revision=1,
            )
        else:
            usage = observation_from_interaction(
                {
                    "id": "owned-interaction",
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "cachedContentTokenCount": 0,
                        "candidatesTokenCount": 50,
                        "thoughtsTokenCount": 0,
                    },
                },
                ref,
                revision=1,
                observed_at=datetime.now(UTC),
                root_turn="owned-root",
                model=ModelRef(provider=provider, id=model),
            )
        port = OfflineNativePort(usage)
        monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: port)
        pricing = ControlledTurnPricing(
            "mock-budget-rates-100-usd-per-million-and-no-infrastructure",
            model,
            provider_price=ProviderPrice(
                provider=provider,
                model=model,
                checked_on=datetime.now(UTC).date(),
                input=Decimal(100),
                output=Decimal(100),
                cache_read=Decimal(100),
            ),
            infrastructure_usd=Decimal(0),
        )
    try:
        yield Context(
            ChannelAccounting(db_session_factory, SCOPE, "discord", CHANNEL.channel_id),
            deps,
            prepared,
            pricing,
            binding,
            provider,
            port,
        )
    finally:
        await drain_outcomes()
        await client.close()


async def test_real_host_turn_meters_once_and_exhausts_literal_budget(context):
    receipt = await context.run()
    if (
        not receipt.metered
        or receipt.before.debits
        or len(receipt.new_debits) != 1
        or receipt.new_debits[0].delta_usd != Decimal("-0.015000")
        or receipt.after.net_debit_usd != Decimal("0.015000")
        or receipt.after.budget.budget.limit_usd != Decimal("0.01")
        or not receipt.after.budget.is_exceeded
        or receipt.after.budget.remaining_usd != 0
    ):
        raise RuntimeError(f"actual billing/readback failed: {receipt.issues}")
    assert "TINY-OK" in str(receipt.outcome.state.content)
    assert len(receipt.after.usage) == 1
    assert receipt.after.usage[0].channel_id == CHANNEL.channel_id
    async with context.reader.sessions() as db:
        assert await tenant_ledger.get_balance(db, tenant_id=TENANT) == Decimal("-0.015000")
    if context.port is not None:
        assert len(context.port.sends) == 1
        assert all(stream.closed for stream in context.port.streams)
        assert context.port.usage.native_meter
        assert all(frame.type != "span.model_request_end" for frame in context.port.display)


async def test_replay_does_not_mint_another_debit(context):
    first = await context.run()
    second = await context.run(operation_key="owned-budget-replay")
    assert first.metered and not second.metered
    assert second.new_debits == ()
    assert "EXPECTED_ONE_NEW_CHANNEL_DEBIT" in second.issues
    assert len(second.after.debits) == 1 and second.after.net_debit_usd == Decimal("0.015000")


@pytest.mark.parametrize("change_identity", ["other_id", "same_id_wrong_root"])
async def test_wrong_root_real_debit_cannot_certify_this_run(context, monkeypatch, change_identity):
    if context.port is None:
        pytest.skip("native-root attribution control")
    own = context.port.usage
    updates = {"turn_id": "other-root"}
    if change_identity == "other_id":
        updates["id"] = "other-observation"
    other = own.model_copy(update=updates)
    actual_record = channel_accounting.record_provider_usage

    async def substitute_unrelated_root(**kwargs):
        kwargs["observation"] = other
        return await actual_record(**kwargs)

    monkeypatch.setattr(channel_accounting, "record_provider_usage", substitute_unrelated_root)
    receipt = await context.run()
    if (
        receipt.metered
        or "PERSISTED_RUN_USAGE_IDENTITY_MISMATCH" not in receipt.issues
        or receipt.after.net_debit_usd != Decimal("0.015000")
        or len(receipt.new_debits) != 1
        or receipt.after.native_usage[0].observation.turn_id != "other-root"
    ):
        raise RuntimeError("unrelated-root production charge satisfied the run proof")


async def test_missing_native_root_identity_cannot_certify_a_charge(context):
    if context.port is None:
        pytest.skip("native-root attribution control")
    context.port.usage = context.port.usage.model_copy(update={"turn_id": None})
    receipt = await context.run()
    if receipt.metered or "AMBIGUOUS_OR_MISSING_RUN_USAGE_IDENTITY" not in receipt.issues:
        raise RuntimeError("unattributed native charge satisfied the run proof")


async def test_two_observed_native_roots_with_one_debit_are_ambiguous(context, monkeypatch):
    if context.port is None:
        pytest.skip("native-root attribution control")
    own = context.port.usage
    if context.provider == "openai":
        unknown = openai_observation(
            {"id": "unknown-root"}, context.prepared.session_ref, revision=1
        )
    else:
        unknown = observation_from_interaction(
            {"id": "unknown-interaction"},
            context.prepared.session_ref,
            revision=1,
            observed_at=datetime.now(UTC),
            root_turn="unknown-root",
            model=ModelRef(provider=context.provider, id=context.pricing.model_id),
        )

    async def two_root_stream(**kwargs):
        stream = NativeStream(
            [
                TurnEvent(native=context.port.display[0]),
                TurnEvent(usage=own),
                TurnEvent(usage=unknown),
                TurnEvent(native=context.port.display[-1]),
            ]
        )
        context.port.streams.append(stream)
        return stream

    async def two_root_usage():
        return (own, unknown)

    monkeypatch.setattr(context.port, "open_stream", two_root_stream)
    monkeypatch.setattr(context.port, "replay_usage", two_root_usage)
    receipt = await context.run()
    if (
        receipt.metered
        or "AMBIGUOUS_OR_MISSING_RUN_USAGE_IDENTITY" not in receipt.issues
        or len(receipt.new_debits) != 1
        or receipt.after.net_debit_usd != Decimal("0.015000")
        or len(receipt.after.native_usage) != 2
    ):
        raise RuntimeError("ambiguous roots with one actual debit satisfied the run proof")


async def test_readback_uses_current_budget_and_is_independent_of_receipt(context):
    await context.run()
    async with context.reader.sessions() as db, db.begin():
        await channel_budgets.delete_channel_budget(
            db, tenant_id=TENANT, platform="discord", channel_id=CHANNEL.channel_id
        )
    report = await context.reader.snapshot()
    assert report.budget is None and len(report.debits) == 1
    assert report.net_debit_usd == Decimal("0.015000")


async def test_unknown_native_infrastructure_retains_pending_usage(context):
    if context.provider == "anthropic":
        pytest.skip("legacy has no infrastructure component")
    receipt = await context.run(pricing=replace(context.pricing, infrastructure_usd=None))
    assert not receipt.metered and not receipt.new_debits
    assert receipt.after.net_debit_usd == 0 and not receipt.after.budget.is_exceeded
    assert len(await context.deps.state_store.pending_outbox()) == 1


async def test_no_usage_frame_is_not_a_metered_turn(context):
    if context.port is None:
        pytest.skip("native usage-only port negative")
    context.port.usage = None
    receipt = await context.run()
    assert not receipt.metered and receipt.after.debits == ()


async def test_removing_the_actual_billing_writer_cannot_pass(context, monkeypatch):
    async def no_write(**kwargs):
        return False

    name = "record_turn_usage" if context.provider == "anthropic" else "record_provider_usage"
    monkeypatch.setattr(channel_accounting, name, no_write)
    receipt = await context.run()
    if receipt.metered or receipt.new_debits or receipt.after.usage:
        raise RuntimeError("a host reply without the production debit was accepted")
    assert "TINY-OK" in str(receipt.outcome.state.content)
    assert not receipt.after.budget.is_exceeded


async def test_cancelled_host_turn_is_not_a_successful_first_turn(context):
    cancel = asyncio.Event()
    cancel.set()
    receipt = await context.run(cancel=cancel)
    assert not receipt.metered
    assert "HOST_TURN_NOT_SUCCESSFUL_SINGLE_ATTEMPT" in receipt.issues
    # The real driver reconciles any provider-reported usage even on failure.
    # Our native fixture has a measured replay snapshot: retain that genuine
    # charge, without mistaking the cancelled turn for completed NEW15 work.
    if context.provider == "anthropic":
        assert not receipt.new_debits
    elif len(receipt.new_debits) != 1 or receipt.after.net_debit_usd != Decimal("0.015000"):
        raise RuntimeError("cancelled native usage lost its actual measured debit")


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "id", "kind"])
async def test_foreign_prepared_native_scope_refused_before_db_or_send(context, field):
    ref = context.prepared.session_ref
    if ref is None:
        ref = ResourceRef(
            id=context.prepared.ma_session_id,
            kind="session",
            provider="anthropic",
            account_scope_id="workspace",
            tenant_id=SCOPE.tenant_id,
            account_id=SCOPE.account_id,
        )
    forged = ref.model_copy(update={field: "foreign"})
    with pytest.raises(ValueError, match="authorized prepared session"):
        await context.run(prepared=replace(context.prepared, session_ref=forged))
    assert not (await context.reader.snapshot()).entries


@pytest.mark.parametrize("change", ["channel", "account", "model", "factory", "thread"])
async def test_mismatched_context_refused_before_native_send(context, change):
    kwargs = {}
    if change == "channel":
        kwargs["prepared"] = replace(
            context.prepared,
            admission=replace(context.prepared.admission, budget_channel_id="foreign"),
        )
    elif change == "account":
        kwargs["prepared"] = replace(context.prepared, session_account_id=uuid.UUID(int=999))
    elif change == "model":
        kwargs["pricing"] = ControlledTurnPricing(
            "mock", "wrong", legacy_rates=ModelRates(1, 1, 1, 1)
        )
    elif change == "factory":
        kwargs["deps"] = replace(
            context.deps, sessionmaker=async_sessionmaker(context.reader.sessions.kw["bind"])
        )
    else:
        kwargs["thread_id"] = ""
    with pytest.raises(ValueError):
        await context.run(**kwargs)
    assert (await context.reader.snapshot()).debits == ()
    if context.port is not None:
        assert context.port.sends == []


async def test_read_only_transaction_rejects_write(context, monkeypatch):
    async def wrong_read(db, **kwargs):
        await db.execute(text("DELETE FROM tenant_ledger"))
        return []

    monkeypatch.setattr(tenant_ledger, "list_for_tenant", wrong_read)
    with pytest.raises(Exception, match="read-only transaction"):
        await context.reader.snapshot()


async def test_snapshot_has_only_unlocked_reads_and_honors_scope(context):
    await context.run()
    statements = []
    engine = context.reader.sessions.kw["bind"]

    def record(conn, cursor, statement, params, execution, many):
        statements.append(statement.upper())

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        report = await context.reader.snapshot()
        other = await replace(context.reader, channel_id="another-channel").snapshot()
        tenant = await replace(
            context.reader, scope=SCOPE.model_copy(update={"tenant_id": str(uuid.UUID(int=998))})
        ).snapshot()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert report.net_debit_usd == Decimal("0.015000")
    assert not other.entries and not other.usage and other.net_debit_usd == 0
    assert not tenant.entries and not tenant.usage and tenant.net_debit_usd == 0
    assert all(s.startswith(("SELECT", "SET TRANSACTION")) for s in statements)
    assert all("ADVISORY" not in s and "FOR UPDATE" not in s for s in statements)


async def test_live_and_incoherent_bind_refusal_precedes_connection(context):
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_production")
    own = context.reader.sessions.kw["bind"]
    try:
        for kwargs in ({"bind": foreign}, {"bind": own, "binds": {AsyncSession: foreign}}):
            with pytest.raises(ValueError):
                ChannelAccounting(
                    async_sessionmaker(**kwargs), SCOPE, "discord", CHANNEL.channel_id
                )
        context.reader.sessions.configure(bind=foreign)
        with pytest.raises(ValueError):
            await context.reader.snapshot()
        with pytest.raises(ValueError):
            await context.run()
    finally:
        context.reader.sessions.configure(bind=own, binds=None)
        await foreign.dispose()


async def test_budget_window_and_low_decimal_precision_readback(context):
    await context.run()
    now = datetime.now(UTC)
    async with context.reader.sessions() as db, db.begin():
        await channel_budgets.set_channel_budget(
            db,
            tenant_id=TENANT,
            platform="discord",
            channel_id=CHANNEL.channel_id,
            limit_usd=Decimal("0.01"),
            window="fixed",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=2),
            set_by_account_id=None,
        )
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        report = await context.reader.snapshot(now=now)
        if report.net_debit_usd != Decimal("0.015000") or report.budget.spent_usd != 0:
            raise RuntimeError("readback rounded the actual ledger or ignored its window")
    assert not report.budget.is_active and not report.budget.is_exceeded
    with pytest.raises(ValueError, match="timezone aware"):
        await context.reader.snapshot(now=datetime(2026, 1, 1))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
@pytest.mark.parametrize("field", ["input", "output", "cache_write", "cache_read"])
def test_invalid_mock_tariffs_refused(bad, field):
    rates = replace(ModelRates(1, 1, 1, 1), **{field: bad})
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ControlledTurnPricing("explicit-mock-policy", "model", legacy_rates=rates)


@pytest.mark.parametrize(
    "change", ["missing_evidence", "missing_model", "no_tariff", "negative_infra", "unknown_infra"]
)
def test_invalid_or_missing_mock_policy_refused(change):
    args = dict(evidence_id="mock", model_id="model", legacy_rates=ModelRates(1, 1, 1, 1))
    if change == "missing_evidence":
        args["evidence_id"] = " "
    elif change == "missing_model":
        args["model_id"] = " "
    elif change == "no_tariff":
        args["legacy_rates"] = None
    elif change == "negative_infra":
        args["infrastructure_usd"] = Decimal(-1)
    else:
        args["infrastructure_usd"] = Decimal("NaN")
    with pytest.raises(ValueError):
        ControlledTurnPricing(**args)

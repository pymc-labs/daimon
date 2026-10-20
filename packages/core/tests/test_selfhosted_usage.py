"""Selfhosted Azure tariff admission, honest settlement and replay recovery."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core.config import SelfhostedBillingSettings, SelfhostedModelPrice, Settings
from daimon.core.selfhosted_usage import (
    record_selfhosted_usage,
    require_selfhosted_price,
    selfhosted_prices,
)
from daimon.core.stores import mux_state, tenant_ledger, usage_events
from daimon.core.stores.accounting_outbox import list_pending_usage
from daimon.core.stores.turn_outcomes import list_for_tenant
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import TurnObservation, current_outcome, drain_outcomes
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID(int=86)
NOW = datetime.now(UTC)


def billing():
    # Deliberately synthetic rates, not a claim about Azure's current prices.
    return SelfhostedBillingSettings(
        deployments={"coke-production": "underlying-model"},
        models={
            "underlying-model": SelfhostedModelPrice(
                checked_on=date(2026, 1, 1),
                input=Decimal("2"),
                output=Decimal("10"),
                cache_read=Decimal("0.5"),
            )
        },
    )


def observation(**changes):
    return UsageObservation.model_validate(
        {
            "id": "selfhosted:request:request-1",
            "revision": 1,
            "session": ResourceRef(
                id="session",
                kind="session",
                provider="selfhosted",
                account_scope_id="harness",
                tenant_id=str(TENANT),
            ),
            "turn_id": "turn-1",
            "model": ModelRef(provider="selfhosted", id="underlying-model"),
            "grain": "model_request",
            "basis": "increment",
            "input_tokens": 1000,
            "input_cached_tokens": 200,
            "output_tokens": 100,
            "output_reasoning_tokens": 60,
            "native_meter": {"request_id": "request-1"},
            "completeness": "measured",
            "observed_at": NOW,
            **changes,
        }
    )


def test_arbitrary_deployments_price_the_explicit_underlying_model():
    settings = billing()
    price = require_selfhosted_price(settings, "coke-production")
    assert price.model == "coke-production"
    assert price.input == Decimal("2")
    assert selfhosted_prices(settings)[("selfhosted", "underlying-model")].output == 10


@pytest.mark.parametrize("case", ["unconfigured", "unpriced", "expired", "future", "model-only"])
def test_unpriced_turn_is_refused(case):
    settings = billing()
    if case == "unconfigured":
        settings.deployments.clear()
    elif case == "unpriced":
        settings.models.clear()
    elif case == "expired":
        settings.models["underlying-model"] = settings.models["underlying-model"].model_copy(
            update={"valid_before": NOW.date()}
        )
    elif case == "future":
        settings.models["underlying-model"] = settings.models["underlying-model"].model_copy(
            update={"checked_on": NOW.date() + timedelta(days=1)}
        )
    deployment = "underlying-model" if case == "model-only" else "coke-production"
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_selfhosted_price(settings, deployment)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_or_free_model_rates_are_rejected(value):
    with pytest.raises(ValidationError):
        SelfhostedModelPrice(checked_on=NOW.date(), input=value, output="1", cache_read="0")


def test_deployment_cannot_shadow_another_underlying_model_tariff():
    settings = billing()
    settings.deployments["underlying-model"] = "some-other-model"
    with pytest.raises(ValueError, match="ambiguous"):
        selfhosted_prices(settings)


def test_nested_environment_settings(monkeypatch):
    monkeypatch.setenv("DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS", '{"blue":"gpt-example"}')
    monkeypatch.setenv(
        "DAIMON_BILLING__SELFHOSTED__MODELS",
        '{"gpt-example":{"checked_on":"2026-01-01","input":"2","output":"10","cache_read":"0.5"}}',
    )
    settings = Settings(database={"url": "postgresql+asyncpg://localhost/unused"})
    assert require_selfhosted_price(settings.billing.selfhosted, "blue").input == 2


@pytest_asyncio.fixture
async def sessions(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        await make_tenant(session, id=TENANT)
    await mux_state.PostgresStateStore(sm).put_binding(
        ProviderBinding(
            id="selfhosted-binding",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id=str(TENANT), platform="teams", channel_id="channel"),
                thread_id="thread",
            ),
            provider="selfhosted",
            profile="selfhosted.loop",
            native_refs={"session": "session"},
            generation=1,
            config_revision=1,
        ),
        expected_generation=0,
    )
    return sm


async def record(sessions, usage, **changes):
    return await record_selfhosted_usage(
        **{
            "sessionmaker": sessions,
            "binding_id": "selfhosted-binding",
            "observation": usage,
            "tenant_id": TENANT,
            "platform_user_id": "user",
            "billing": billing(),
            "deployment": "coke-production",
            "infrastructure_usd": Decimal(0),
            "markup": Decimal("1.5"),
            "channel_id": "channel",
            **changes,
        }
    )


async def balance(sessions):
    async with sessions() as session:
        return await tenant_ledger.get_balance(session, tenant_id=TENANT)


async def pending(sessions, tenant=TENANT):
    async with sessions() as session:
        return await list_pending_usage(session, tenant_id=tenant)


@pytest.mark.parametrize("model", ["underlying-model", "coke-production"])
async def test_metered_request_debits_once_with_cached_and_reasoning_subsets(sessions, model):
    usage = observation(model=ModelRef(provider="selfhosted", id=model))
    results = await asyncio.gather(record(sessions, usage), record(sessions, usage))
    assert sorted(results) == [False, True]
    # (800 * 2 + 200 * .5 + 100 * 10) / 1M * 1.5; reasoning is within output.
    assert await balance(sessions) == Decimal("-0.004050")
    assert not await record(sessions, usage)
    assert await balance(sessions) == Decimal("-0.004050")
    assert await pending(sessions) == []
    async with sessions() as session:
        rows = await usage_events.list_for_tenant(session, tenant_id=TENANT)
    assert len(rows) == 1
    assert rows[0].input_tokens == 800 and rows[0].cache_read_input_tokens == 200


@pytest.mark.parametrize("counts", [True, False])
async def test_unknown_usage_is_visible_and_held_until_measured_revision(sessions, counts):
    usage = observation(
        completeness="unknown",
        **(
            {}
            if counts
            else {"input_tokens": None, "input_cached_tokens": None, "output_tokens": None}
        ),
    )
    outcome = TurnObservation(sessions, TENANT, "teams", session_id="session")
    token = current_outcome.set(outcome)
    try:
        assert not await record(sessions, usage)
    finally:
        current_outcome.reset(token)
    outcome.finish()
    await drain_outcomes()
    assert await balance(sessions) == 0
    gaps = await pending(sessions)
    assert len(gaps) == 1 and gaps[0].completeness == "unknown"
    assert gaps[0].observation_id == usage.id
    assert await pending(sessions, uuid.UUID(int=87)) == []
    async with sessions() as session:
        outcomes = await list_for_tenant(session, TENANT)
    assert outcomes[0].cost_usd is None and outcomes[0].unpriced_calls == 1
    assert await record(sessions, observation(revision=2))
    assert await balance(sessions) == Decimal("-0.004050")
    assert not await record(sessions, observation(revision=2))
    # A superseded pending row may be acknowledged, without another debit.
    await record(sessions, usage)
    assert await balance(sessions) == Decimal("-0.004050")
    assert await pending(sessions) == []


async def test_missing_infrastructure_and_unexpected_model_remain_pending(sessions):
    assert not await record(sessions, observation(), infrastructure_usd=None)
    assert await balance(sessions) == 0
    assert len(await pending(sessions)) == 1
    assert await record(sessions, observation())
    foreign = observation(id="request-2", model=ModelRef(provider="selfhosted", id="surprise"))
    assert not await record(sessions, foreign)
    assert await balance(sessions) == Decimal("-0.004050")
    gaps = await pending(sessions)
    assert len(gaps) == 1 and gaps[0].model == "surprise"


async def test_postrun_tariff_removal_preserves_receipt_for_repair(sessions):
    assert not await record(sessions, observation(), billing=SelfhostedBillingSettings())
    assert await balance(sessions) == 0
    assert len(await pending(sessions)) == 1
    assert await record(sessions, observation())
    assert await balance(sessions) == Decimal("-0.004050")


async def test_expired_tariff_can_still_reconcile_usage_within_its_verified_window(sessions):
    settings = billing()
    settings.models["underlying-model"] = settings.models["underlying-model"].model_copy(
        update={"valid_before": NOW.date()}
    )
    with pytest.raises(AdmissionDenied):
        require_selfhosted_price(settings, "coke-production")
    historical = observation(observed_at=NOW - timedelta(days=1))
    assert await record(sessions, historical, billing=settings)
    assert await balance(sessions) == Decimal("-0.004050")

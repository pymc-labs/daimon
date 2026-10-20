"""Selfhosted Azure tariff admission, honest settlement and replay recovery."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.core.config import SelfhostedBillingSettings, SelfhostedModelPrice, Settings
from daimon.core.selfhosted_usage import (
    admit_selfhosted_usage,
    record_selfhosted_usage,
    require_selfhosted_price,
    selfhosted_prices,
)
from daimon.core.stores import mux_state, tenant_ledger, usage_events
from daimon.core.stores import selfhosted_usage as contexts
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
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_billing_clock(monkeypatch):
    # Fix both admission and lookup time, so UTC midnight cannot move a tariff window.
    import daimon.core.selfhosted_usage as metering

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(metering, "datetime", Clock)


def billing():
    # Deliberately synthetic rates, not a claim about Azure's current prices.
    return SelfhostedBillingSettings(
        deployments={"coke-production": "underlying-model"},
        models={
            "underlying-model": [
                SelfhostedModelPrice(
                    effective_from=date(2026, 1, 1),
                    input=Decimal("2"),
                    output=Decimal("10"),
                    cache_read=Decimal("0.5"),
                )
            ]
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
        settings.models["underlying-model"][0] = settings.models["underlying-model"][0].model_copy(
            update={"valid_before": NOW.date()}
        )
    elif case == "future":
        settings.models["underlying-model"][0] = settings.models["underlying-model"][0].model_copy(
            update={"effective_from": NOW.date() + timedelta(days=1)}
        )
    deployment = "underlying-model" if case == "model-only" else "coke-production"
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_selfhosted_price(settings, deployment)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_or_free_model_rates_are_rejected(value):
    with pytest.raises(ValidationError):
        SelfhostedModelPrice(effective_from=NOW.date(), input=value, output="1", cache_read="0")


def test_deployment_cannot_shadow_another_underlying_model_tariff():
    settings = billing()
    settings.deployments["underlying-model"] = "some-other-model"
    with pytest.raises(ValueError, match="priced underlying model"):
        selfhosted_prices(settings)


def test_nested_environment_settings(monkeypatch):
    monkeypatch.setenv("DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS", '{"blue":"gpt-example"}')
    monkeypatch.setenv(
        "DAIMON_BILLING__SELFHOSTED__MODELS",
        '{"gpt-example":[{"effective_from":"2026-01-01","input":"2","output":"10","cache_read":"0.5"}]}',
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
    await admit(sm)
    return sm


async def admit(sessions, *, settings=None, key="turn-admission-1", deployment="coke-production"):
    return await admit_selfhosted_usage(
        sessionmaker=sessions,
        tenant_id=TENANT,
        binding_id="selfhosted-binding",
        session_ref=observation().session,
        admission_key=key,
        deployment=deployment,
        billing=settings or billing(),
    )


async def record(sessions, usage, **changes):
    return await record_selfhosted_usage(
        **{
            "sessionmaker": sessions,
            "binding_id": "selfhosted-binding",
            "observation": usage,
            "tenant_id": TENANT,
            "platform_user_id": "user",
            "admission_key": "turn-admission-1",
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
        return (await list_pending_usage(session, tenant_id=tenant)).rows


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


async def test_missing_admission_preserves_receipt_for_reconciliation(sessions):
    assert not await record(sessions, observation(), admission_key=None)
    assert await balance(sessions) == 0
    gaps = await pending(sessions)
    assert gaps[0].reason == "admission_missing"


async def test_deployment_remap_never_changes_admitted_tariff_or_corrections(sessions):
    original = await admit(sessions)
    settings = billing()
    settings.models["new-model"] = [
        SelfhostedModelPrice(
            effective_from=date(2026, 1, 1),
            input="20",
            output="100",
            cache_read="5",
        )
    ]
    settings.deployments["coke-production"] = "new-model"
    # Idempotent recovery loads original admission, even with a remapped deployment.
    recovered = await admit(sessions, settings=settings)
    assert recovered == original
    assert recovered.underlying_model == "underlying-model"
    usage = observation(model=ModelRef(provider="selfhosted", id="coke-production"))
    assert not await record(sessions, usage, infrastructure_usd=None)
    assert await record(sessions, usage)
    assert await balance(sessions) == Decimal("-0.004050")
    assert await record(sessions, usage.model_copy(update={"revision": 2, "output_tokens": 200}))
    assert await balance(sessions) == Decimal("-0.005550")
    assert not await record(sessions, usage)


async def test_rate_change_and_removal_cannot_reprice_pending_receipt(sessions):
    original = await admit(sessions)
    assert not await record(sessions, observation(), infrastructure_usd=None)
    settings = billing()
    settings.models["underlying-model"].append(
        SelfhostedModelPrice(
            effective_from=NOW.date(),
            input="20",
            output="100",
            cache_read="5",
        )
    )
    recovered = await admit(sessions, settings=settings)
    assert recovered.tariff == original.tariff
    # A newly admitted turn sees the new rate. Old receipts never consult it.
    fresh = await admit(sessions, settings=settings, key="new-turn")
    assert fresh.tariff.input == Decimal("20")
    assert await record(sessions, observation())
    assert await balance(sessions) == Decimal("-0.004050")
    assert await record(
        sessions, observation(revision=2, output_tokens=200), admission_key="new-turn"
    )
    assert await balance(sessions) == Decimal("-0.005550")
    assert (await admit(sessions, settings=SelfhostedBillingSettings())).tariff == original.tariff


@pytest.mark.parametrize("case", ["dangling", "clash", "duplicate", "overlap"])
def test_invalid_tariff_config_refuses_startup(case):
    config = billing().model_dump()
    if case == "dangling":
        config["deployments"]["blue"] = "missing"
    elif case == "clash":
        config["models"]["blue"] = config["models"]["underlying-model"]
        config["deployments"]["blue"] = "underlying-model"
    elif case == "duplicate":
        config["models"]["underlying-model"] *= 2
    else:
        config["models"]["underlying-model"][0]["valid_before"] = date(2027, 1, 1)
        config["models"]["underlying-model"].append(
            {
                "effective_from": NOW.date(),
                "input": "3",
                "output": "15",
                "cache_read": "1",
            }
        )
    with pytest.raises(ValidationError):
        SelfhostedBillingSettings.model_validate(config)


def test_history_selects_effective_windows_and_gaps():
    settings = billing()
    settings.models["underlying-model"][0] = settings.models["underlying-model"][0].model_copy(
        update={"valid_before": date(2026, 6, 1)}
    )
    settings.models["underlying-model"].append(
        SelfhostedModelPrice(
            effective_from=date(2026, 7, 1),
            input="3",
            output="15",
            cache_read="1",
        )
    )
    assert (
        selfhosted_prices(settings, on=date(2026, 5, 1))[("selfhosted", "coke-production")].input
        == 2
    )
    assert not selfhosted_prices(settings, on=date(2026, 6, 15))
    assert (
        selfhosted_prices(settings, on=date(2026, 7, 1))[("selfhosted", "coke-production")].input
        == 3
    )


async def test_bad_pricing_context_does_not_drop_the_native_receipt(sessions):
    from daimon.core._models import MuxOperation
    from sqlalchemy import select, update

    key = contexts.context_key("admission", "selfhosted-binding", "turn-admission-1")
    async with sessions() as db, db.begin():
        value = await contexts.read_context(
            db, tenant_id=TENANT, binding_id="selfhosted-binding", key=key
        )
        value["tariff"] = {"invalid": True}
        # Simulate corrupted historical tariff metadata, not a fake recorder.
        from mux.state.operations import OperationRecord

        body = await db.scalar(select(MuxOperation.record).where(MuxOperation.key == key))
        saved = OperationRecord.model_validate(body)
        saved = saved.model_copy(update={"result": value})
        await db.execute(
            update(MuxOperation)
            .where(MuxOperation.key == key)
            .values(record=saved.model_dump(mode="json"))
        )
    assert not await record(sessions, observation())
    assert await balance(sessions) == 0
    gaps = await pending(sessions)
    assert gaps[0].reason == "tariff_context_invalid"
    captured = await mux_state.PostgresStateStore(sessions).pending_outbox(limit=10)
    assert captured[0].observation == observation()


async def test_pagination_reaches_beyond_one_thousand_pending_requests(sessions):
    async with sessions() as db, db.begin():
        for index in range(1005):
            await mux_state.record_usage(
                db,
                "selfhosted-binding",
                observation(
                    id=f"pending-{index:04}",
                    completeness="unknown",
                ),
            )
    async with sessions() as db:
        first = await list_pending_usage(db, tenant_id=TENANT, limit=1000)
        assert first.truncated and first.next_cursor is not None
        assert len(first.rows) == 1000
        second = await list_pending_usage(
            db, tenant_id=TENANT, limit=1000, cursor=first.next_cursor
        )
        assert not second.truncated and second.next_cursor is None
        assert len(second.rows) == 5
        assert len({row.observation_id for row in first.rows + second.rows}) == 1005
        with pytest.raises(ValueError, match="another tenant"):
            await list_pending_usage(db, tenant_id=uuid.UUID(int=87), cursor=first.next_cursor)
        with pytest.raises(ValueError, match="invalid"):
            await list_pending_usage(db, tenant_id=TENANT, cursor="not a cursor")


async def test_bad_row_is_flagged_without_hiding_other_pending_rows(sessions):
    from daimon.core._models import AccountingOutbox
    from sqlalchemy import update

    assert not await record(sessions, observation(), infrastructure_usd=None)
    assert not await record(sessions, observation(id="good-row", completeness="unknown"))
    async with sessions() as db, db.begin():
        await db.execute(
            update(AccountingOutbox)
            .where(
                AccountingOutbox.observation_id == observation().id,
            )
            .values(row={"invalid": "body"})
        )
    async with sessions() as db:
        page = await list_pending_usage(db, tenant_id=TENANT)
    assert len(page.rows) == 2 and page.invalid_rows == 1
    assert {row.reason for row in page.rows} == {"invalid_observation", "usage_unknown"}
    assert next(row for row in page.rows if row.reason == "invalid_observation").session_id is None

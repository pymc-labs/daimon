"""Independent worker meters roll up read-only without extra root debits."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest
import pytest_asyncio
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import mux_state, tenant_ledger
from daimon.core.usage_delegation import (
    HostUsageBinding,
    read_delegated_usage,
)
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID(int=818)
NOW = datetime(2026, 10, 12, tzinfo=UTC)
SCOPE = Scope(
    tenant_id=str(TENANT), account_id="root-account", principal_id="host", authorization_id="owned"
)


def member(provider):
    native = ResourceRef(
        provider=provider,
        kind="session",
        id=f"{provider}-session",
        account_scope_id="owned-workspace",
        tenant_id=str(TENANT),
        account_id=SCOPE.account_id,
    )
    return HostUsageBinding(
        ProviderBinding(
            id=f"{provider}-binding",
            provider=provider,
            profile="mock",
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=str(TENANT), platform="slack", channel_id="root-channel"
                ),
                thread_id=f"fleet/run/{provider}",
            ),
            native_refs={"session": native.id},
            generation=1,
            config_revision=0,
        ),
        native,
    )


MEMBERS = tuple(member(provider) for provider in ("anthropic", "openai", "gemini"))
RATES = ModelRates(input=2, output=10, cache_write=3, cache_read=1)
PRICES = {
    (m.session.provider, "mock-model"): RATES
    if m.session.provider == "anthropic"
    else ProviderPrice(
        m.session.provider,
        "mock-model",
        NOW.date(),
        Decimal(2),
        Decimal(10),
        Decimal(1),
        Decimal(3),
    )
    for m in MEMBERS
}


def usage(m, *, revision=1, inputs=100, outputs=10):
    return UsageObservation(
        id="native-observation",
        revision=revision,
        session=m.session,
        model=ModelRef(provider=m.session.provider, id="mock-model"),
        turn_id="native-turn",
        grain="model_request" if m.session.provider == "anthropic" else "turn",
        basis="cumulative",
        input_tokens=inputs,
        input_cached_tokens=20,
        input_cache_write_tokens=5,
        output_tokens=outputs,
        observed_at=NOW,
        completeness="partial" if outputs is None else "measured",
        native_meter={"mock_offline": True},
    )


@pytest_asyncio.fixture
async def sessions(db_preflight, db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db, db.begin():
        await make_tenant(db, id=TENANT)
        for m in MEMBERS:
            await mux_state.put_binding(db, m.binding, expected_generation=0)
    return sm


async def record(sm, m, value):
    async with sm() as db, db.begin():
        return await record_observation_usage(
            db,
            binding_id=m.binding.id,
            observation=value,
            tenant_id=TENANT,
            platform_user_id="worker",
            pricing=RATES,
            provider_price=PRICES[(value.model.provider, value.model.id)]
            if m.session.provider != "anthropic"
            else None,
            infrastructure_usd=Decimal(".03"),
            channel_id="root-channel",
            billing_grain=value.grain,
        )


async def read(sm, bindings=MEMBERS, scope=SCOPE):
    async with sm() as db, db.begin():
        return await read_delegated_usage(db, scope=scope, bindings=bindings, prices=PRICES)


async def ledger(sm):
    async with sm() as db:
        return await tenant_ledger.list_for_tenant(db, tenant_id=TENANT)


async def test_three_providers_keep_original_meters_and_single_debits(sessions):
    values = tuple(usage(m) for m in MEMBERS)
    for m, value in zip(MEMBERS, values, strict=True):
        assert await record(sessions, m, value)
        assert not await record(sessions, m, value)
    before = await ledger(sessions)
    with localcontext() as context:
        context.prec = 6
        result = await read(sessions)
    assert result.observations == values
    assert result.token_floor_usd == Decimal(".000855")
    assert result.ledger_debit_usd == Decimal(".060855")
    assert (result.unpriced_observations, result.pending_revisions) == (0, 0)
    assert len(before) == 3 and await ledger(sessions) == before
    assert all(row.channel_id == "root-channel" for row in before)
    # No synthesized root/session aggregate and no fourth orchestrator debit.
    assert {v.session for v in result.observations} == {m.session for m in MEMBERS}


async def test_revisions_replace_not_add_and_reads_do_not_apply_pending(sessions):
    m = MEMBERS[2]
    assert not await record(sessions, m, usage(m, outputs=None))
    pending = await read(sessions, (m,))
    assert pending.token_floor_usd == Decimal(".000185")
    assert pending.pending_revisions == 1 and pending.ledger_debit_usd == 0
    assert await ledger(sessions) == []
    assert await record(sessions, m, usage(m, revision=2, inputs=200))
    result = await read(sessions, (m,))
    assert result.token_floor_usd == Decimal(".000485")
    assert result.ledger_debit_usd == Decimal(".030485")
    assert len(result.observations) == 1 and result.observations[0].revision == 2
    # A settled latest revision does not turn an old pending row into finality.
    assert result.pending_revisions == 1


@pytest.mark.parametrize(
    "defect", ["tenant", "account", "duplicate", "session", "binding", "private_account"]
)
async def test_attribution_refuses_scope_or_duplicate_before_any_read(defect):
    m = MEMBERS[0]
    bindings = (m,)
    if defect in ("tenant", "account"):
        m = replace(m, session=m.session.model_copy(update={f"{defect}_id": "foreign"}))
        bindings = (m,)
    elif defect == "duplicate":
        bindings = (m, m)
    elif defect == "session":
        bindings = (replace(m, session=m.session.model_copy(update={"id": "another"})),)
    elif defect == "private_account":
        bindings = (
            replace(m, binding=m.binding.model_copy(update={"legacy_account_id": "foreign"})),
        )
    else:
        bindings = (replace(m, binding=m.binding.model_copy(update={"provider": "gemini"})),)

    class NoRead:
        def __getattr__(self, name):
            raise AssertionError("identity validation must precede DB access")

    with pytest.raises((ScopeViolation, ValueError)):
        await read_delegated_usage(NoRead(), scope=SCOPE, bindings=bindings, prices=PRICES)


async def test_unpriced_worker_read_keeps_valid_sibling_floor(sessions):
    for m in MEMBERS:
        assert await record(sessions, m, usage(m))
    async with sessions() as db, db.begin():
        result = await read_delegated_usage(
            db,
            scope=SCOPE,
            bindings=MEMBERS,
            prices={
                (MEMBERS[0].session.provider, "mock-model"): RATES,
            },
        )
    assert result.token_floor_usd == Decimal(".000285")
    assert result.unpriced_observations == 2
    assert result.ledger_debit_usd == Decimal(".060855")


async def test_cross_worker_sums_remain_exact_at_low_ambient_precision(sessions):
    for m in MEMBERS:
        assert await record(sessions, m, usage(m, inputs=100_000_000, outputs=10_000_000))
    with localcontext() as context:
        context.prec = 6
        result = await read(sessions)
    assert result.token_floor_usd == Decimal("899.999955")
    assert result.ledger_debit_usd == Decimal("900.059955")

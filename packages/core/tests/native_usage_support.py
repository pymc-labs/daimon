"""Committed ordinary bindings and pending usage, without orchestration fixtures."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from daimon.core._models import Account
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.pricing import ModelRates, provider_prices
from daimon.core.stores import mux_state
from daimon.testing.factories import make_account, make_ledger_entry, make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def sm(db_preflight, db_engine, db_clean):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def seed_binding(sm, *, provider="gemini", tenant_id=None):
    async with sm.begin() as db:
        tenant = await make_tenant(db) if tenant_id is None else None
        tenant_id = tenant.id if tenant is not None else tenant_id
        if tenant is None:
            account = Account(tenant_id=tenant_id)
            db.add(account)
            await db.flush()
        else:
            account = await make_account(db, tenant=tenant)
        ident = str(uuid4())
        binding = ProviderBinding(
            id=ident,
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=str(tenant_id), platform="discord", channel_id="channel"
                ),
                thread_id=ident,
            ),
            legacy_account_id=str(account.id),
            provider=provider,
            profile="gemini.inline_reuse"
            if provider == "gemini"
            else "openai.persistent_workspace",
            native_refs={"session": "native-" + ident},
            generation=1,
            config_revision=0,
        )
        await mux_state.put_binding(db, binding, expected_generation=0)
        return binding


async def pending_usage(sm, *, provider="gemini", outputs=100):
    binding = await seed_binding(sm, provider=provider)
    model = "gemini-3.8-flash" if provider == "gemini" else "gpt-6-luna"
    observation = UsageObservation(
        id="meter",
        session=ResourceRef(
            id=binding.native_refs["session"],
            kind="session",
            provider=provider,
            account_scope_id="owned-workspace",
            tenant_id=binding.thread.channel.tenant_id,
            account_id=binding.legacy_account_id,
        ),
        revision=1,
        model=ModelRef(provider=provider, id=model),
        turn_id="turn",
        grain="turn",
        basis="cumulative",
        input_tokens=1000,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=outputs,
        observed_at=datetime.now(UTC),
        completeness="partial" if outputs is None else "measured",
        native_meter={"offline": True},
    )
    async with sm.begin() as db:
        await record_observation_usage(
            db,
            binding_id=binding.id,
            observation=observation,
            tenant_id=UUID(binding.thread.channel.tenant_id),
            platform_user_id="tester",
            pricing=ModelRates(2, 10, 3, 1),
            provider_price=provider_prices(binding.profile)[(provider, model)],
            infrastructure_usd=None,
            billing_grain="turn",
            channel_id="channel",
        )
    return binding


async def seed_ledger(sm):
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        await make_account(db, tenant=tenant)
        row = await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(0))
    return row

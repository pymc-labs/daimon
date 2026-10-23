"""Each OpenAI model has a daily spend cap per tenant: the guard where no native cap exists."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.pricing import ProviderPrice, minimum_cost_of
from daimon.core.stores import mux_state
from daimon.core.stores.openai_spend import openai_spend_today, utc_midnight
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.openai_host import (
    _check_daily_model_cap,  # pyright: ignore[reportPrivateUsage]
)
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT, OTHER = (
    uuid.UUID("4401aaaa-0000-4000-8000-000000004401"),
    uuid.UUID("4402bbbb-0000-4000-8000-000000004402"),
)
NOW = datetime(2026, 10, 23, 13, tzinfo=UTC)
ASTRA = ProviderPrice(
    "openai", "gpt-6-astra", NOW.date(), Decimal(10), Decimal(50), Decimal(1), Decimal(10)
)
LUNA = ProviderPrice(
    "openai",
    "gpt-6-luna",
    NOW.date(),
    Decimal("0.1"),
    Decimal("0.5"),
    Decimal("0.01"),
    Decimal("0.1"),
)
PRICES = {"gpt-6-astra": ASTRA, "gpt-6-luna": LUNA}


def binding(tenant: uuid.UUID, name: str) -> tuple[ProviderBinding, ResourceRef]:
    native = ResourceRef(
        provider="openai",
        kind="session",
        id=f"{name}-session",
        account_scope_id="daimon-deployment",
        tenant_id=str(tenant),
        account_id="account",
    )
    return (
        ProviderBinding(
            id=f"{name}-binding",
            provider="openai",
            profile="openai.persistent_workspace",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id=str(tenant), platform="discord", channel_id="c"),
                thread_id=name,
            ),
            native_refs={"session": native.id},
            generation=1,
            config_revision=0,
        ),
        native,
    )


BINDINGS = {
    "astra": binding(TENANT, "astra"),
    "luna": binding(TENANT, "luna"),
    "other": binding(OTHER, "other"),
}


def usage(
    session: ResourceRef, model: str, *, at: datetime = NOW, id_: str = "obs"
) -> UsageObservation:
    return UsageObservation(
        id=id_,
        revision=1,
        session=session,
        model=ModelRef(provider="openai", id=model),
        turn_id="turn",
        grain="turn",
        basis="cumulative",
        input_tokens=200_000,
        output_tokens=100_000,
        observed_at=at,
        completeness="measured",
    )


@pytest_asyncio.fixture
async def sessions(db_preflight, db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as db, db.begin():
        await make_tenant(db, id=TENANT)
        await make_tenant(db, id=OTHER)
        for bound, _ in BINDINGS.values():
            await mux_state.put_binding(db, bound, expected_generation=0)
    return sm


async def record(sm, name: str, value: UsageObservation) -> None:
    bound, _ = BINDINGS[name]
    async with sm() as db, db.begin():
        await record_observation_usage(
            db,
            binding_id=bound.id,
            observation=value,
            tenant_id=uuid.UUID(bound.thread.channel.tenant_id),
            platform_user_id="person",
            pricing=None,
            provider_price=PRICES[value.model.id],
            # As on every OpenAI turn today: infrastructure unknown, no ledger debit.
            infrastructure_usd=None,
            channel_id="c",
            billing_grain="turn",
        )


async def spent(sm, model: str, *, tenant: uuid.UUID = TENANT, now: datetime = NOW) -> Decimal:
    async with sm() as db:
        return await openai_spend_today(
            db,
            tenant_id=tenant,
            provider="openai",
            model=model,
            now=now,
            rates=PRICES[model],
            markup=Decimal(1),
        )


async def test_today_counts_only_this_tenants_model_since_midnight_utc(sessions):
    astra_session = BINDINGS["astra"][1]
    await record(sessions, "astra", usage(astra_session, "gpt-6-astra"))
    await record(
        sessions,
        "astra",
        usage(astra_session, "gpt-6-astra", at=utc_midnight(NOW) - timedelta(minutes=1), id_="y"),
    )
    await record(sessions, "luna", usage(BINDINGS["luna"][1], "gpt-6-luna"))
    await record(sessions, "other", usage(BINDINGS["other"][1], "gpt-6-astra"))

    expected = minimum_cost_of(usage(astra_session, "gpt-6-astra"), ASTRA)
    assert expected is not None and expected > 0
    assert await spent(sessions, "gpt-6-astra") == expected, "yesterday and others excluded"
    assert await spent(sessions, "gpt-6-astra", tenant=OTHER) == expected
    assert await spent(sessions, "gpt-6-astra", now=NOW + timedelta(days=1)) == 0, "it resets"


def _request(sm, *, cap: Decimal | None, worker: bool = False) -> Any:
    return SimpleNamespace(
        tenant_id=TENANT,
        worker=SimpleNamespace() if worker else None,
        now=lambda: NOW,
        deps=SimpleNamespace(openai_daily_model_cap_usd=cap, sessionmaker=sm, markup=Decimal(1)),
    )


def _revision(model: str) -> Any:
    return SimpleNamespace(backend="openai", model=model)


def runtime(model: str) -> Any:
    return cast(Any, SimpleNamespace(price=PRICES[model]))


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-luna"])
async def test_the_cap_refuses_a_model_at_its_limit_and_leaves_others_alone(sessions, model):
    name = "astra" if model == "gpt-6-astra" else "luna"
    await record(sessions, name, usage(BINDINGS[name][1], model))
    used = await spent(sessions, model)

    with pytest.raises(AdmissionDenied) as refused:
        await _check_daily_model_cap(_request(sessions, cap=used), runtime(model), _revision(model))
    assert (refused.value.reason, refused.value.provider) == ("provider_daily_cap_reached", model)

    await _check_daily_model_cap(_request(sessions, cap=used + 1), runtime(model), _revision(model))
    other = "gpt-6-luna" if model == "gpt-6-astra" else "gpt-6-astra"
    await _check_daily_model_cap(_request(sessions, cap=used), runtime(other), _revision(other))


async def test_a_worker_may_use_the_cap_exactly(sessions):
    """Its own reservation already counts toward today's spend."""
    await record(sessions, "astra", usage(BINDINGS["astra"][1], "gpt-6-astra"))
    used = await spent(sessions, "gpt-6-astra")
    await _check_daily_model_cap(
        _request(sessions, cap=used, worker=True), runtime("gpt-6-astra"), _revision("gpt-6-astra")
    )


async def test_no_cap_configured_checks_nothing(sessions):
    await _check_daily_model_cap(
        _request(sessions, cap=None), runtime("gpt-6-astra"), _revision("gpt-6-astra")
    )


def test_the_refusal_names_the_model_and_when_it_resets():
    text = admission_refusal_text(
        "provider_daily_cap_reached", RefusalNouns.for_platform("discord"), provider="gpt-6-astra"
    )
    assert "Today's GPT-6 Astra budget for this server is used up." in text
    assert "midnight UTC" in text and "DAIMON_" not in text


async def test_an_expired_or_missing_tariff_refuses_instead_of_counting_nothing(sessions):
    expired = ProviderPrice(
        "openai",
        "gpt-6-astra",
        NOW.date() - timedelta(days=30),
        Decimal(10),
        Decimal(50),
        Decimal(1),
        Decimal(10),
        valid_before=NOW.date() - timedelta(days=1),
    )
    for price in (expired, None):
        with pytest.raises(AdmissionDenied) as refused:
            await _check_daily_model_cap(
                _request(sessions, cap=Decimal(100)),
                cast(Any, SimpleNamespace(price=price)),
                _revision("gpt-6-astra"),
            )
        assert refused.value.reason == "provider_daily_cap_reached"


async def test_a_malformed_usage_row_is_skipped_not_fatal(sessions, monkeypatch):
    from daimon.core.stores import openai_spend

    await record(sessions, "astra", usage(BINDINGS["astra"][1], "gpt-6-astra"))

    def broken(*_args, **_kwargs):
        raise ValueError("cached tokens exceed input")

    monkeypatch.setattr(openai_spend, "provider_cost_of", broken)
    assert await spent(sessions, "gpt-6-astra") == 0


def test_a_gemini_refusal_names_the_gemini_model():
    text = admission_refusal_text(
        "provider_daily_cap_reached",
        RefusalNouns.for_platform("discord"),
        provider="gemini-4-argon",
    )
    assert "Today's Gemini 4 Argon budget for this server is used up." in text


@pytest.mark.parametrize("count", [0, 2])
def test_a_gemini_grounding_call_is_logged_as_a_leak(count):
    import structlog.testing
    from daimon.core.turn.gemini_prepare import (
        _warn_on_grounding,  # pyright: ignore[reportPrivateUsage]
    )

    session = ResourceRef(
        provider="gemini",
        kind="session",
        id="g-session",
        account_scope_id="daimon-deployment",
        tenant_id=str(TENANT),
        account_id="account",
    )
    observation = UsageObservation(
        id="gemini:one:usage",
        revision=1,
        session=session,
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        grain="turn",
        basis="cumulative",
        input_tokens=10,
        output_tokens=5,
        native_meter={"total_input_tokens": 10, "grounding_tool_count": count},
        completeness="measured",
        observed_at=NOW,
    )
    with structlog.testing.capture_logs() as logs:
        _warn_on_grounding(observation, tenant_id=TENANT, binding_id="b")
    leaks = [entry for entry in logs if entry["event"] == "turn.gemini_grounding_unexpected"]
    assert len(leaks) == (1 if count else 0)
    if count:
        assert leaks[0]["grounding_tool_count"] == count and leaks[0]["observation_id"]

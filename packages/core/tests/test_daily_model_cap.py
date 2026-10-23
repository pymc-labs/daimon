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
from daimon.core.stores.model_spend import model_spend_today, utc_midnight
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
        return await model_spend_today(
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
        assert refused.value.reason == "provider_price_unavailable"


async def test_a_malformed_usage_row_is_skipped_not_fatal(sessions, monkeypatch):
    from daimon.core.stores import model_spend

    await record(sessions, "astra", usage(BINDINGS["astra"][1], "gpt-6-astra"))

    def broken(*_args, **_kwargs):
        raise ValueError("cached tokens exceed input")

    monkeypatch.setattr(model_spend, "provider_cost_of", broken)
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
    """The real usage shape: a list of `{type, count}` per grounding tool."""
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
        native_meter={
            "total_input_tokens": 10,
            "grounding_tool_count": [
                {"type": "google_search", "count": count},
                {"type": "url_context", "count": 0},
            ],
        },
        completeness="measured",
        observed_at=NOW,
    )
    with structlog.testing.capture_logs() as logs:
        _warn_on_grounding(observation, tenant_id=TENANT, binding_id="b")
    leaks = [entry for entry in logs if entry["event"] == "turn.gemini_grounding_unexpected"]
    assert len(leaks) == (1 if count else 0)
    if count:
        assert leaks[0]["grounding_tool_count"] == count and leaks[0]["observation_id"]
        assert leaks[0]["grounding_types"] == ["google_search", "url_context"]


class _Inner:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def before_attempt(self, *, model, attempt) -> None:
        self.attempts.append(model.id)

    async def after_attempt(self, *, receipt) -> None:
        return None


class _Source:
    """The primary answers 503; any other create is recorded."""

    def __init__(self) -> None:
        self.created: list[str] = []

    async def create(self, request):
        from mux.errors import ProviderError

        model = request["agent_config"]["model"]
        self.created.append(model)
        if model == "gemini-3.8-flash":
            raise ProviderError("overloaded", retryable=True, native_code="503")
        return {"id": "interaction", "status": "in_progress"}


def _flash_request() -> dict[str, Any]:
    return {
        "agent": "antigravity",
        "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
    }


async def test_a_flash_fallback_past_its_cap_is_refused_before_it_is_created(monkeypatch):
    """A 503 on the primary moves down the chain; each fallback model is capped too."""
    from daimon.core.turn import daily_model_cap
    from daimon.core.turn.daily_model_cap import CappedFlashAccounting
    from mux.drivers.gemini.flash import FlashTransport

    checked: list[str] = []

    async def cap(_request, *, provider, model, price, cap):
        checked.append(model)
        if model != "gemini-3.8-flash":
            raise AdmissionDenied(reason="provider_daily_cap_reached", provider=model)

    monkeypatch.setattr(daily_model_cap, "check_daily_model_cap", cap)
    inner, source = _Inner(), _Source()
    accounting = CappedFlashAccounting(inner, cast(Any, None), {}, Decimal(20))
    with pytest.raises(AdmissionDenied) as refused:
        await FlashTransport(cast(Any, source), accounting).create(_flash_request())

    assert refused.value.provider == "gemini-flash-latest"
    assert source.created == ["gemini-3.8-flash"], "the fallback was never created"
    assert checked == ["gemini-3.8-flash", "gemini-flash-latest"]
    assert inner.attempts == ["gemini-3.8-flash"], "no hold for the refused fallback"


async def test_a_fallback_without_a_tariff_never_runs_and_the_503_stands():
    """gemini-flash-latest has no dated tariff: never run uncounted, never 'unpriced'."""
    from contextlib import asynccontextmanager

    from daimon.core.turn.daily_model_cap import CappedFlashAccounting

    @asynccontextmanager
    async def no_db():
        yield None

    request = SimpleNamespace(
        tenant_id=TENANT,
        worker=None,
        now=lambda: NOW,
        deps=SimpleNamespace(sessionmaker=no_db, markup=Decimal(1)),
    )
    from mux.errors import ProviderError

    inner = _Inner()
    accounting = CappedFlashAccounting(inner, cast(Any, request), {}, Decimal(20))
    with pytest.raises(ProviderError) as busy:
        await accounting.before_attempt(
            model=ModelRef(provider="gemini", id="gemini-flash-latest"), attempt=2
        )
    assert (busy.value.category, busy.value.native_code) == ("overloaded", "503")
    assert inner.attempts == [], "no hold for a fallback that never runs"
    # The turn's own model without a tariff is still refused plainly.
    with pytest.raises(AdmissionDenied) as refused:
        await accounting.before_attempt(
            model=ModelRef(provider="gemini", id="gemini-flash-latest"), attempt=1
        )
    assert (refused.value.reason, refused.value.provider) == (
        "provider_price_unavailable",
        "gemini-flash-latest",
    )


async def test_a_continuation_is_never_refused_halfway(monkeypatch):
    """Once the turn's first request is accepted, later creates skip the cap check."""
    from daimon.core.turn import daily_model_cap
    from daimon.core.turn.daily_model_cap import CappedFlashAccounting
    from mux.drivers.gemini.flash import FlashAttempt

    checked: list[int] = []

    async def cap(_request, *, provider, model, price, cap):
        checked.append(1)

    monkeypatch.setattr(daily_model_cap, "check_daily_model_cap", cap)
    inner = _Inner()
    accounting = CappedFlashAccounting(inner, cast(Any, None), {}, Decimal(20))
    flash = ModelRef(provider="gemini", id="gemini-3.8-flash")
    await accounting.before_attempt(model=flash, attempt=1)
    await accounting.after_attempt(
        receipt=FlashAttempt(model=flash, attempt=1, outcome="accepted", interaction_id="i1")
    )
    await accounting.before_attempt(model=flash, attempt=1)  # a tool-result continuation
    assert checked == [1]
    assert inner.attempts == ["gemini-3.8-flash", "gemini-3.8-flash"]


async def test_holds_settle_through_the_capped_accounting(monkeypatch):
    """`HoldSettlement` is a runtime Protocol: the wrapper must satisfy it itself."""
    from daimon.core.pricing import provider_prices
    from daimon.core.turn import gemini_prepare
    from daimon.core.turn.daily_model_cap import CappedFlashAccounting
    from daimon.core.turn.gemini_prepare import _settle_hold  # pyright: ignore[reportPrivateUsage]
    from daimon.core.turn.gemini_state import HoldSettlement

    class _Settling(_Inner):
        def __init__(self) -> None:
            super().__init__()
            self.settled: list[tuple[str, str]] = []

        async def settle(self, *, interaction_id, model, actual_usd) -> bool:
            self.settled.append((interaction_id, model))
            return True

    inner = _Settling()
    accounting = CappedFlashAccounting(inner, cast(Any, None), {}, Decimal(20))
    assert isinstance(accounting, HoldSettlement)
    price = {model: p for (_, model), p in provider_prices("gemini.inline_reuse").items()}[
        "gemini-3.8-flash"
    ]
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
        completeness="measured",
        observed_at=NOW,
        final=True,
    )
    monkeypatch.setattr(gemini_prepare, "provider_cost_of", lambda *_a, **_k: Decimal("0.01"))
    await _settle_hold(accounting, observation, price)
    assert inner.settled == [("one", "gemini-3.8-flash")]

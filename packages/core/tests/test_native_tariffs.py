"""Published Standard tariffs preserve exact-versus-floor semantics offline."""

import os
import socket
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal, localcontext
from pathlib import Path

import httpx
import pytest
from daimon.core import pricing
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation


@pytest.fixture(autouse=True)
def fixed_source_date(monkeypatch):
    class CheckedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return datetime(2026, 10, 14, tzinfo=UTC)

    monkeypatch.setattr(pricing, "datetime", CheckedClock)


def usage(price, **changes):
    return UsageObservation(
        **dict(
            id="usage",
            revision=1,
            session=ResourceRef(
                provider=price.provider,
                kind="session",
                id="session",
                tenant_id="tenant",
                account_scope_id="account",
            ),
            model=ModelRef(provider=price.provider, id=price.model),
            grain="session",
            basis="cumulative",
            observed_at=datetime(2026, 10, 14, tzinfo=UTC),
            completeness="measured",
            native_meter={},
            input_tokens=100,
            input_cached_tokens=10,
            input_cache_write_tokens=0,
            output_tokens=20,
        )
        | changes
    )


@pytest.mark.parametrize(
    "profile,model,expected",
    [
        ("openai.persistent_workspace", "gpt-6-luna", (".10", ".50", ".01", ".125")),
        ("gemini.inline_reuse", "gemini-3.5-flash-lite", (".30", "2.50", ".03", None)),
        ("gemini.inline_reuse", "gemini-3.8-flash", (".75", "3.75", ".075", None)),
    ],
)
def test_official_rows_are_shared_immutable_and_dated(profile, model, expected):
    rows = pricing.provider_prices(profile)
    price = next(value for value in rows.values() if value.model == model)
    assert (price.input, price.output, price.cache_read, price.cache_write) == tuple(
        None if value is None else Decimal(value) for value in expected
    )
    assert price.checked_on == date(2026, 10, 14)
    assert pricing.provider_prices(profile)[(price.provider, model)] is price
    with pytest.raises(TypeError):
        rows[(price.provider, "unknown")] = price
    with pytest.raises(FrozenInstanceError):
        price.input = Decimal(0)


def test_unsupported_models_and_profiles_are_absent():
    assert not pricing.provider_prices("anthropic.managed_agents")
    assert not pricing.provider_prices("unknown.profile")
    assert ("gemini", "gemini-pro") not in pricing.provider_prices("gemini.inline_reuse")


def test_luna_context_and_unknown_write_keep_exact_actual_pending():
    price = pricing.provider_prices("openai.persistent_workspace")[("openai", "gpt-6-luna")]
    assert pricing.provider_cost_of(usage(price), price, infrastructure_usd=Decimal(0)) == Decimal(
        ".0000191"
    )
    assert (
        pricing.provider_cost_of(
            usage(price, input_tokens=272_000), price, infrastructure_usd=Decimal(0)
        )
        is not None
    )
    aggregate = usage(price, input_tokens=272_001)
    assert pricing.provider_cost_of(aggregate, price, infrastructure_usd=Decimal(0)) is None
    assert pricing.minimum_cost_of(aggregate, price) == Decimal(".0272092")
    assert (
        pricing.provider_cost_of(
            usage(price, input_cache_write_tokens=None), price, infrastructure_usd=Decimal(0)
        )
        is None
    )
    assert pricing.provider_cost_of(usage(price), price, infrastructure_usd=None) is None


def test_gemini_exact_rates_do_not_double_count_reasoning_or_erase_unknown_runtime():
    price = pricing.provider_prices("gemini.inline_reuse")[("gemini", "gemini-3.5-flash-lite")]
    value = usage(price, output_reasoning_tokens=15)
    with localcontext() as arithmetic:
        arithmetic.prec = 6
        assert pricing.provider_cost_of(
            value, price, infrastructure_usd=Decimal(".000123456789")
        ) == Decimal(".000200756789")
    assert pricing.provider_cost_of(value, price, infrastructure_usd=None) is None
    assert (
        pricing.provider_cost_of(
            usage(price, input_cached_tokens=None), price, infrastructure_usd=Decimal(0)
        )
        is None
    )


def test_published_expiry_and_future_source_refuse_floor_and_exact_cost(monkeypatch):
    price = pricing.provider_prices("gemini.inline_reuse")[("gemini", "gemini-3.8-flash")]
    for observed in (datetime(2026, 10, 13, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)):
        value = usage(price, observed_at=observed)
        assert pricing.minimum_cost_of(value, price) is None
        assert pricing.provider_cost_of(value, price, infrastructure_usd=Decimal(0)) is None

    class AfterPromotion(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return datetime(2027, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(pricing, "datetime", AfterPromotion)
    rows = pricing.provider_prices("gemini.inline_reuse")
    assert ("gemini", "gemini-3.8-flash") not in rows
    assert ("gemini", "gemini-3.5-flash-lite") in rows


@pytest.mark.parametrize(
    "changes",
    [
        {"max_priced_input_tokens": -1},
        {"max_priced_input_tokens": True},
        {"valid_before": date(2026, 10, 14)},
    ],
)
def test_invalid_applicability_refuses(changes):
    price = pricing.provider_prices("openai.persistent_workspace")[("openai", "gpt-6-luna")]
    with pytest.raises(ValueError):
        replace(price, **changes)


def test_owner_supplier_is_cold_without_environment_file_or_transport_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("tariff supplier attempted environment or transport I/O")

    with monkeypatch.context() as cold:
        cold.setattr(os, "getenv", forbidden)
        cold.setattr(Path, "read_text", forbidden)
        cold.setattr(Path, "read_bytes", forbidden)
        cold.setattr(socket, "socket", forbidden)
        cold.setattr(httpx, "Client", forbidden)
        cold.setattr(httpx, "AsyncClient", forbidden)
        assert len(pricing.provider_prices("openai.persistent_workspace")) == 1
        assert len(pricing.provider_prices("gemini.inline_reuse")) == 2
        assert not pricing.provider_prices("unknown.profile")


def test_supplier_identity_comes_from_profiles_and_uses_utc_not_host_date(monkeypatch):
    from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE

    class HostDate(date):
        @classmethod
        def today(cls):
            raise AssertionError("supplier consulted host-local date")

    monkeypatch.setattr(pricing, "date", HostDate)
    for profile in (INLINE_REUSE, PERSISTENT_WORKSPACE):
        rows = pricing.provider_prices(profile.profile_id)
        assert rows
        assert all(
            key == (profile.provider, price.model) and price.provider == profile.provider
            for key, price in rows.items()
        )


def _clock(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # pyright: ignore[reportIncompatibleMethodOverride]
            assert tz is UTC
            return datetime(day.year, day.month, day.day, tzinfo=UTC)

    monkeypatch.setattr(pricing, "datetime", Clock)


def test_argon_intro_tariff_prices_cached_input_at_five_percent_of_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock(monkeypatch, date(2026, 11, 30))
    price = pricing.provider_prices("gemini.inline_reuse")[("gemini", "gemini-4-argon")]
    assert (price.input, price.output, price.cache_read, price.cache_write) == (
        Decimal("2.00"),
        Decimal("10.00"),
        Decimal("0.10"),
        None,
    )
    assert price.cache_read == price.input * Decimal("0.05")
    assert (price.checked_on, price.valid_before) == (date(2026, 10, 22), date(2026, 12, 1))
    # 100 inclusive input with 10 cached: 90 x $2 + 10 x $0.10 + 20 output x $10, per 1M.
    value = usage(price, observed_at=datetime(2026, 11, 30, tzinfo=UTC))
    assert pricing.provider_cost_of(value, price, infrastructure_usd=Decimal(0)) == Decimal(
        ".000381"
    )
    all_cached = usage(
        price, input_cached_tokens=100, observed_at=datetime(2026, 11, 30, tzinfo=UTC)
    )
    assert pricing.provider_cost_of(all_cached, price, infrastructure_usd=Decimal(0)) == Decimal(
        ".00021"
    )


def test_argon_has_no_price_outside_its_intro_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, date(2026, 11, 30))
    price = pricing.provider_prices("gemini.inline_reuse")[("gemini", "gemini-4-argon")]
    for observed in (datetime(2026, 10, 21, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)):
        value = usage(price, observed_at=observed)
        assert pricing.minimum_cost_of(value, price) is None
        assert pricing.provider_cost_of(value, price, infrastructure_usd=Decimal(0)) is None
    # No post-intro row is invented: from 2026-12-01 Argon is simply unpriced.
    _clock(monkeypatch, date(2026, 12, 1))
    rows = pricing.provider_prices("gemini.inline_reuse")
    assert ("gemini", "gemini-4-argon") not in rows
    assert ("gemini", "gemini-3.8-flash") in rows


def test_argon_expiry_is_reported_only_after_its_window(monkeypatch: pytest.MonkeyPatch) -> None:
    def expired():
        return pricing.provider_price_expired_on("gemini.inline_reuse", "gemini", "gemini-4-argon")

    _clock(monkeypatch, date(2026, 11, 30))
    assert expired() is None
    _clock(monkeypatch, date(2026, 12, 1))
    assert expired() == date(2026, 11, 30)
    assert pricing.provider_price_expired_on("gemini.inline_reuse", "gemini", "unknown") is None
    assert (
        pricing.provider_price_expired_on("gemini.inline_reuse", "gemini", "gemini-3.5-flash-lite")
        is None
    )

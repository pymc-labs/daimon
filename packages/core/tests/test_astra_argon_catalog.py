"""gpt-6-astra and gemini-4-argon: priced models an agent's own config may pick."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.channel_backend import agent_selection, check_backend
from daimon.core.neutral_resources import WorkerSpecRefused, resolve_worker_spec
from daimon.core.pricing import PROVIDER_PRICING_SOURCES, provider_cost_of, provider_prices
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.profiles import (
    backend_selection,
    runnable_selection,
    selectable_models,
    worker_selection_allowed,
)
from mux.profiles.gemini import ARGON_MODEL
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE

from .test_fleet_permissions import NOW, POLICY, parent

ASTRA = "gpt-6-astra"
CHANNEL = ChannelRef(tenant_id="t", platform="discord", channel_id="c")


def astra(profile: str = PERSISTENT_WORKSPACE.profile_id) -> ConfigRevision:
    return ConfigRevision.create(
        CHANNEL, 1, resolve_default(BackendConfig(backend="openai", profile=profile, model=ASTRA))
    )


def test_astra_is_an_agent_config_model_that_also_runs_fleet_workers() -> None:
    # An agent's config picks its model; a fleet then runs that agent as a worker.
    assert selectable_models("openai") == ("gpt-6-luna", ASTRA)
    assert agent_selection("openai", ASTRA).model == ASTRA
    for profile in (PERSISTENT_WORKSPACE.profile_id, CONVERSATION_ONLY.profile_id):
        revision = astra(profile)
        assert runnable_selection(revision) and worker_selection_allowed(revision)
        check_backend(revision)
        check_backend(revision, worker=True)


def test_argon_is_an_agent_config_model_that_also_runs_fleet_workers() -> None:
    assert ARGON_MODEL in selectable_models("gemini")
    selection = agent_selection("gemini", ARGON_MODEL)
    revision = ConfigRevision.create(CHANNEL, 1, selection)
    # No longer channel-only: a worker running an Argon agent is admitted (bounded).
    assert worker_selection_allowed(revision)
    check_backend(revision)
    check_backend(revision, worker=True)


def test_astra_has_a_dated_verified_price_row() -> None:
    price = provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)]
    assert (price.input, price.output, price.cache_read, price.cache_write) == (
        Decimal("10.00"),
        Decimal("50.00"),
        Decimal("1.00"),
        Decimal("12.50"),
    )
    assert price.max_priced_input_tokens == 272_000
    assert "https://developers.openai.com/api/docs/models/gpt-6-astra" in PROVIDER_PRICING_SOURCES


def usage(**changes: object) -> UsageObservation:
    fields: dict[str, object] = dict(
        id="usage",
        revision=1,
        session=ResourceRef(
            provider="openai",
            kind="session",
            id="session",
            tenant_id="tenant",
            account_scope_id="account",
        ),
        model=ModelRef(provider="openai", id=ASTRA),
        grain="session",
        basis="cumulative",
        observed_at=datetime(2026, 10, 22, tzinfo=UTC),
        completeness="measured",
        native_meter={},
        input_tokens=100_000,
        input_cached_tokens=20_000,
        input_cache_write_tokens=0,
        output_tokens=10_000,
    )
    return UsageObservation.model_validate(fields | changes)


def test_astra_usage_is_priced_at_its_rate_and_conservatively_above_272k() -> None:
    price = provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)]
    # 80k uncached at $10 + 20k cached at $1 + 10k output at $50, per million.
    assert provider_cost_of(usage(), price, infrastructure_usd=Decimal(0)) == Decimal("1.32")
    # Above 272k input OpenAI doubles input/cache and charges 1.5x output. The whole
    # aggregate is priced at those rates (an upper bound), never at the base rate:
    # 272,001 uncached at $20 + 10k output at $75, per million.
    assert provider_cost_of(
        usage(input_tokens=272_001, input_cached_tokens=0),
        price,
        infrastructure_usd=Decimal(0),
    ) == Decimal("6.19002")
    # At exactly the threshold, the base rate.
    assert provider_cost_of(
        usage(input_tokens=272_000, input_cached_tokens=0),
        price,
        infrastructure_usd=Decimal(0),
    ) == Decimal("3.22")


def test_long_context_multipliers_need_a_threshold() -> None:
    with pytest.raises(ValueError, match="long-context"):
        replace(
            provider_prices(PERSISTENT_WORKSPACE.profile_id)[("openai", ASTRA)],
            max_priced_input_tokens=None,
        )


@pytest.mark.parametrize("granted", (True, False))
def test_astra_worker_spec_resolves_only_when_granted(granted: bool) -> None:
    root = parent("openai")
    model = ModelRef(provider="openai", id=ASTRA)
    selection = backend_selection("openai").model_copy(update={"model": ASTRA})
    if granted:
        root = replace(
            root,
            models=(*root.models, model),
            backend_grants=(*root.backend_grants, selection),
        )
    spec = root.spec.model_copy(update={"model": model})
    backend = BackendConfig(backend="openai", profile=selection.profile, model=ASTRA)
    if granted:
        resolved = resolve_worker_spec(root, spec, backend=backend, policy=POLICY, now=NOW)
        assert resolved.backend.model == ASTRA and resolved.spec.model == model
    else:
        with pytest.raises(WorkerSpecRefused) as refused:
            resolve_worker_spec(root, spec, backend=backend, policy=POLICY, now=NOW)
        assert (refused.value.field, refused.value.reason) == ("backend", "not_subset")

"""Granted native preparation pass-through with pre-connection fixture admission."""

from dataclasses import replace

import pytest
from daimon.core.turn.openai_host import prepare_openai
from daimon.core.turn.run import prepared_billing
from daimon.core.usage_billing import ObservationBilled

from . import test_gemini_preparation as gemini
from . import test_openai_host as openai

host = gemini.host
composed = openai.composed


@pytest.fixture
def owned_gemini(db_preflight, host):
    return host


@pytest.fixture
def owned_openai(db_preflight, composed):
    return composed


@pytest.mark.parametrize("enabled", [False, True])
async def test_gemini_preparation_passes_fresh_cost_context_only_when_enabled(
    owned_gemini, enabled
):
    owned_gemini.deps = replace(owned_gemini.deps, native_footer_cost=enabled)
    first = await gemini.prepare(owned_gemini)
    second = await gemini.prepare(owned_gemini)
    assert (first.native_footer_cost is not None) == enabled
    assert (second.native_footer_cost is not None) == enabled
    if enabled:
        assert first.native_footer_cost is not second.native_footer_cost
        assert first.native_footer_cost.session == first.session_ref
        billing = prepared_billing(first, tenant_id=gemini.TENANT)
        assert isinstance(billing, ObservationBilled)
        assert billing.native_footer_cost is first.native_footer_cost


@pytest.mark.parametrize("enabled", [False, True])
async def test_openai_preparation_passes_fresh_cost_context_only_when_enabled(
    owned_openai, enabled
):
    request, wire, store, observations, runtime = owned_openai
    request = replace(request, deps=replace(request.deps, native_footer_cost=enabled))
    first = await prepare_openai(request)
    second = await prepare_openai(request)
    assert (first.native_footer_cost is not None) == enabled
    assert (second.native_footer_cost is not None) == enabled
    if enabled:
        assert first.native_footer_cost is not second.native_footer_cost
        assert first.native_footer_cost.session == first.session_ref
        billing = prepared_billing(first, tenant_id=openai.TENANT)
        assert isinstance(billing, ObservationBilled)
        assert billing.native_footer_cost is first.native_footer_cost

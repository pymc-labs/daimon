"""A Daimon record's MA-shaped facts project back to the same record, provider included."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core.agent_record_facts import agent_facts
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import source_agent_record
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from mux.contracts.ids import ModelRef, Provider, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec
from mux.profiles import selectable_models


@pytest.mark.parametrize("provider", tuple(DEFAULT_KEY_ENV))
def test_facts_round_trip_keeps_the_provider(provider: Provider) -> None:
    """agent_facts then source_agent_record keeps the model and its provider."""
    tenant = "00000000-0000-0000-0000-000000000001"
    model = ModelRef(provider=provider, id=selectable_models(provider)[0])
    record = Agent(
        ref=ResourceRef(provider=provider, kind="agent", id="dm_agent_1", account_scope_id=tenant),
        revision=Revision(local=3),
        spec=AgentSpec(name="analyst", model=model, system="Analyse."),
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
    )
    back = source_agent_record(
        agent_facts(record, tenant_id=tenant), resource_scope(tenant_id=tenant)
    )
    assert back is not None, f"{provider} facts project back to a record"
    assert back.spec.model == model, f"{provider} model survives: {back.spec.model}"
    assert back.spec.system == "Analyse.", "the definition survives the round trip"

"""Daimon-owned records tolerate the seeded agent being renamed to `default`.

The rename is in place (same record id), so code from before it must still
find that record when asked for `daimon`, and must never mistake a workspace's
own `default` agent, or another tenant's, for it.
"""

from __future__ import annotations

import pytest
from daimon.core.defaults.ma_index import find_renamed_seeded_agent
from daimon.core.defaults.metadata import build_metadata
from daimon.core.defaults.owned import owned_scope, reconcile_owned_agent, resolve_owned_agent
from daimon.core.defaults.report import Action
from daimon.core.setup_conversations import resolve_setup_agents
from daimon.core.specs import AgentSpec
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.core.turn.selfhosted_records import names_a_record, resolve_records
from daimon.testing.factories import make_tenant
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response

from ..stores.test_selfhosted_resources import agent_spec, environment_spec

SPEC = AgentSpec(name="daimon", model="claude-sonnet-5-5", system="Help.")


async def _record(sessionmaker, tenant, name: str, *, managed: bool) -> str:
    """A live record `name` in `tenant`, stamped as the defaults sync stamps its own."""
    metadata = build_metadata(tenant_id=tenant.id, name=name, managed=managed)
    created = await SelfHostedAgents(sessionmaker).create(
        owned_scope(tenant.id, None),
        agent_spec(name).model_copy(update={"metadata": metadata}),
        key=f"{tenant.id}:{name}",
    )
    return created.ref.id


async def _lookup_misses(sessionmaker, tenant, name: str = "daimon") -> bool:
    try:
        await resolve_owned_agent(sessionmaker, owned_scope(tenant.id, None), name)
    except ProviderError as error:
        return error.category == "not_found"
    return False


async def test_a_miss_on_daimon_finds_the_managed_default_record(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    seeded = await _record(db_session_factory, tenant, "default", managed=True)
    found = await resolve_owned_agent(db_session_factory, owned_scope(tenant.id, None), "daimon")
    assert found.ref.id == seeded, "`daimon` answers with the seeded record renamed in place"
    assert await names_a_record(db_session_factory, owned_scope(tenant.id, None), "daimon"), (
        "a keyed install must still route `daimon` to its record, not to Managed Agents"
    )


async def test_a_record_named_daimon_wins_over_default(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=True)
    tagged = await _record(db_session_factory, tenant, "daimon", managed=True)
    found = await resolve_owned_agent(db_session_factory, owned_scope(tenant.id, None), "daimon")
    assert found.ref.id == tagged, "an exact `daimon` record is never displaced by the fallback"


async def test_an_unmanaged_default_record_never_matches(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=False)
    assert await _lookup_misses(db_session_factory, tenant), "a user's `default` is not the seed"
    assert not await names_a_record(db_session_factory, owned_scope(tenant.id, None), "daimon")


async def test_another_tenants_default_record_never_matches(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, other, "default", managed=True)
    assert await _lookup_misses(db_session_factory, tenant), "the fallback stays in one tenant"


async def test_only_the_seeded_name_falls_back(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=True)
    assert await _lookup_misses(db_session_factory, tenant, "helper"), (
        "another missing name is a plain miss"
    )


async def test_resolve_records_serves_the_renamed_seed(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    scope = owned_scope(tenant.id, None)
    seeded = await _record(db_session_factory, tenant, "default", managed=True)
    await SelfHostedEnvironments(db_session_factory).create(
        scope, environment_spec(), key="environment"
    )
    agent, _ = await resolve_records(
        db_session_factory,
        scope,
        agent_name="daimon",
        environment_name="default",
        responder_id=None,
    )
    assert agent.id == seeded, "a self-hosted turn for `daimon` runs the renamed record"


async def test_reconcile_skips_beside_the_renamed_record(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    seeded = await _record(db_session_factory, tenant, "default", managed=True)
    outcome = await reconcile_owned_agent(
        db_session_factory,
        SPEC,
        tenant_id=tenant.id,
        dry_run=False,
        account_id=None,
        managed=True,
        create_only=False,
    )
    assert (outcome.action, outcome.anthropic_id) == (Action.SKIPPED, seeded), (
        "a second `daimon` record must not be created beside the renamed one"
    )
    assert await _lookup_misses(db_session_factory, tenant) is False


async def test_reconcile_of_a_users_own_daimon_ignores_the_renamed_record(
    db_session, db_session_factory
):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=True)
    outcome = await reconcile_owned_agent(
        db_session_factory,
        SPEC,
        tenant_id=tenant.id,
        dry_run=True,
        account_id=None,
        managed=False,
        create_only=False,
    )
    assert outcome.action is Action.CREATED, "only the seeded spec is covered by the rename"


async def test_reconcile_of_an_unmanaged_default_still_creates(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=False)
    outcome = await reconcile_owned_agent(
        db_session_factory,
        SPEC,
        tenant_id=tenant.id,
        dry_run=True,
        account_id=None,
        managed=True,
        create_only=False,
    )
    assert outcome.action is Action.CREATED, "a user's `default` does not stand in for `daimon`"


async def test_setup_finds_the_renamed_record_without_an_anthropic_key(
    db_session, db_session_factory
):
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    await db_session.commit()
    seeded = await _record(db_session_factory, tenant, "default", managed=True)
    await _record(db_session_factory, other, "default", managed=True)
    found = await find_renamed_seeded_agent(None, tenant_id=tenant.id, records=db_session_factory)
    assert found is not None and found.id == seeded, "the tenant's own renamed record is found"


@pytest.mark.parametrize("managed", [False, True])
async def test_setup_ignores_an_unmanaged_default_record(db_session, db_session_factory, managed):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _record(db_session_factory, tenant, "default", managed=managed)
    found = await find_renamed_seeded_agent(None, tenant_id=tenant.id, records=db_session_factory)
    assert (found is not None) is managed, "only a managed `default` record is the seeded agent"


async def test_setup_responds_with_the_renamed_record_beside_an_empty_provider(
    db_session, db_session_factory
):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    seeded = await _record(db_session_factory, tenant, "default", managed=True)
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, match: list_response([]))
    responder, target = await resolve_setup_agents(
        build_fake_anthropic(router.dispatch), tenant_id=tenant.id, records=db_session_factory
    )
    assert (responder.id, target) == (seeded, None), "setup runs on the renamed record"

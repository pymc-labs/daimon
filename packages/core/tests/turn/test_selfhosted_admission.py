"""Self-hosted admission must never touch an Anthropic client."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_backend import channel_ref
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.selfhosted_resources import (
    SelfHostedAgents,
    SelfHostedEnvironments,
)
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.turn.admission import AdmissionDenied, admit
from daimon.core.turn.outcomes import drain_outcomes
from daimon.testing.factories import make_account, make_tenant, make_tenant_config
from daimon.testing.turn_fakes import deps_for
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.profiles.selfhosted import LOOP

from ..stores.test_selfhosted_resources import agent_spec, environment_spec, scope


class ForbiddenAnthropic:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"Anthropic access during selfhosted admission: {name}")


@pytest.mark.parametrize("agent_backends", [False, True])
@pytest.mark.parametrize("sealed", [False, True])
async def test_admits_without_anthropic_key_or_calls(
    db_session,
    db_session_factory,
    monkeypatch,
    agent_backends,
    sealed,
):
    monkeypatch.delenv("DAIMON_ANTHROPIC__API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await set_funding_mode(db_session, tenant_id=tenant.id, funding_mode="operator_funded")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            enabled_backends=("anthropic", LOOP.provider),
            channel_rules={"channel": ChannelRule(readers="inside")} if sealed else {},
        ),
    )
    revision = ConfigRevision.create(
        channel_ref(tenant.id, "discord", "channel"),
        1,
        resolve_default(
            BackendConfig(backend=LOOP.provider, profile="selfhosted.loop", model="gpt-6-luna")
        ),
    )
    await mux_state.put_config_revision(db_session, revision)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    agent = await SelfHostedAgents(db_session_factory).create(authorized, agent_spec(), key="agent")
    env = await SelfHostedEnvironments(db_session_factory).create(
        authorized, environment_spec(), key="env"
    )

    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    deps = replace(
        deps_for(ForbiddenAnthropic(), db_session_factory),
        channel_backends=True,
        agent_backends=agent_backends,
    )
    result = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="caller",
        channel_id="channel",
        now=datetime.now(UTC),
    )
    assert result.agent.id == agent.ref.id and result.environment.id == env.ref.id
    assert result.agent.model.id == "gpt-6-luna"
    assert result.backend_revision == revision
    assert result.source_sealed == sealed
    assert result.memory_read_only == sealed
    assert result.grant is not None and "daimon" in result.grant.agent.names
    assert result.channel_skills == ()
    await drain_outcomes()

    # The same local projection still passes through policy and archive gates.
    async with db_session_factory.begin() as db:
        await set_access_policy(
            db,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                enabled_backends=("anthropic", LOOP.provider),
                agent_rules={"daimon": AgentRule(runs_in=("other",))},
            ),
        )
    with pytest.raises(AdmissionDenied, match="runs_elsewhere"):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="caller",
            channel_id="channel",
            now=datetime.now(UTC),
        )
    await SelfHostedAgents(db_session_factory).archive(authorized, agent.ref, key="archive")
    with pytest.raises(MAResolverMissError):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="caller",
            channel_id="channel",
            now=datetime.now(UTC),
        )
    # An archived concrete responder must not erase the tenant's live
    # replacement merely because both records have the same friendly name.
    from daimon.core.scope import TenantScopeRef
    from daimon.core.stores.scoped_config_read import get_scope
    from daimon.core.stores.thread_agent_bindings import create_binding

    replacement = await SelfHostedAgents(db_session_factory).create(
        authorized, agent_spec(), key="replacement"
    )
    assert replacement.ref != agent.ref
    async with db_session_factory.begin() as db:
        await create_binding(
            db,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="bound",
            responder_ma_agent_id=agent.ref.id,
            responder_name="daimon",
        )
    with pytest.raises(MAResolverMissError):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="caller",
            channel_id="channel",
            thread_id="bound",
            now=datetime.now(UTC),
        )
    async with db_session_factory() as db:
        retained = await get_scope(db, scope=TenantScopeRef(tenant_id=tenant.id))
        assert retained is not None and retained.agent_name == "daimon"
    await drain_outcomes()


async def test_bound_responder_is_an_exact_id_never_a_name(db_session, db_session_factory):
    from daimon.core.turn.selfhosted_records import resolve_records

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    await SelfHostedAgents(db_session_factory).create(
        authorized, agent_spec("ag_foreign"), key="namesake"
    )
    await SelfHostedEnvironments(db_session_factory).create(
        authorized, environment_spec(), key="env"
    )
    with pytest.raises(MAResolverMissError):
        await resolve_records(
            db_session_factory,
            authorized,
            agent_name="ag_foreign",
            environment_name="default",
            responder_id="ag_foreign",
        )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("agent_backends", [False, True])
async def test_agent_selected_selfhosted_is_refused_before_anthropic(
    db_session, db_session_factory, monkeypatch, agent_backends
):
    from uuid import uuid4

    from daimon.core.stores import agent_backends as preferences

    monkeypatch.delenv("DAIMON_ANTHROPIC__API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    from sqlalchemy import text

    # Model an invalid legacy row inside this test's isolated DDL schema.
    await db_session.execute(
        text("ALTER TABLE agent_backend_revision DROP CONSTRAINT ck_agent_backend_provider")
    )
    await preferences.put_revision(
        db_session,
        tenant_id=tenant.id,
        agent_id=uuid4(),
        local=1,
        backend=LOOP.provider,
        digest="legacy",
        previous_local=None,
        agent_name="daimon",
        preview_digest="legacy",
    )
    await db_session.commit()
    deps = replace(
        deps_for(ForbiddenAnthropic(), db_session_factory),
        channel_backends=True,
        agent_backends=agent_backends,
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="caller",
            channel_id="channel",
            now=datetime.now(UTC),
        )
    await drain_outcomes()


@pytest.mark.parametrize("kind", ["agent", "environment"])
async def test_invalid_local_records_become_admission_denials(db_session, db_session_factory, kind):
    from daimon.core.turn.selfhosted_records import resolve_records
    from sqlalchemy import text

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    authorized = scope(str(tenant.id), str(account.id))
    await SelfHostedAgents(db_session_factory).create(authorized, agent_spec(), key="agent")
    await SelfHostedEnvironments(db_session_factory).create(
        authorized, environment_spec(), key="env"
    )
    table = "selfhosted_agents" if kind == "agent" else "selfhosted_environments"
    async with db_session_factory.begin() as db:
        await db.execute(text(f"UPDATE {table} SET spec = spec - 'name'"))
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await resolve_records(
            db_session_factory,
            authorized,
            agent_name="daimon",
            environment_name="default",
            responder_id=None,
        )

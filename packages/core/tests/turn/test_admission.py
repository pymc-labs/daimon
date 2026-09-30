"""Unit tests for daimon.core.turn.admission.admit -- the D-01 stage-one
chokepoint: identity -> config cascade -> missing-config bail -> MA
resolve+retrieve -> balance gate -> cap gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.billing import BillingConfig
from daimon.core.config import McpSettings
from daimon.core.direct_messages import start_dm
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import MAResolverMissError, ResolverCache, new_resolver_cache
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import AccessPolicyUnreadable, set_access_policy
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import FundingMode, Role, TenantRow
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.turn.admission import Admission, AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.deps import TurnDeps
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    resolved_agent_env_router,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daimon.testing.factories import (  # isort: skip
    make_channel_budget,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
    make_tenant_user_cap,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _archived_agent_router(*, tenant_id: uuid.UUID, dead_agent: BetaManagedAgentsAgent) -> MARouter:
    """Router with a live environment plus a retrieve-only archived agent.

    Deliberately has no agent LIST route: the resolver's TTL-cache hit
    returns the dead agent's id without ever calling the tag lookup,
    mirroring the real staleness -- an id cached while the agent was live,
    archived since.
    """
    env = ma_environment(id="env_1", name="default", tenant_id=tenant_id)
    router = MARouter()
    router.add_agent(dead_agent)
    router.add_environment_list(env)
    router.add_environment(env)
    return router


def _billing_config() -> BillingConfig:
    return BillingConfig(
        secret_key=SecretStr("sk_test"),
        webhook_secret=SecretStr("whsec_test"),
        prices={10: "price_10", 25: "price_25", 50: "price_50", 100: "price_100"},
        success_url="https://example.com/billing/success",
        cancel_url="https://example.com/billing/cancel",
    )


def _deps(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    router: MARouter,
    billing_config: BillingConfig | None = None,
    resolver_cache: ResolverCache | None = None,
) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=resolver_cache if resolver_cache is not None else new_resolver_cache(),
        defaults_root=defaults_root,
        mcp=McpSettings(),
        billing_config=billing_config,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_admit_over_balance_tenant_raises_admission_denied_balance_depleted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await db_session.commit()
    # No ledger entry seeded -> balance is 0 -> over-balance.

    router = resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=router,
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform=platform,
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.reason == "balance_depleted", (
        "an over-balance tenant must raise AdmissionDenied(reason='balance_depleted')"
    )

    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with db_session_factory() as session:
        outcomes = await list_for_tenant(session, tenant.id)
    assert len(outcomes) == 1
    assert outcomes[0].reason == "admission_balance_depleted"
    assert outcomes[0].platform == platform
    assert outcomes[0].agent_id == "ag_1"
    assert outcomes[0].account_id is not None


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("funding_mode", ["prepaid", "operator_funded"])
async def test_admit_over_cap_user_raises_admission_denied_cap_exceeded(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
    funding_mode: FundingMode,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    if funding_mode == "prepaid":
        await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    else:
        await set_funding_mode(db_session, tenant_id=tenant.id, funding_mode=funding_mode)
    await make_tenant_user_cap(db_session, tenant=tenant, amount=Decimal("0"))
    await db_session.commit()

    router = resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=router,
        billing_config=_billing_config(),
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform=platform,
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.reason == "cap_exceeded", (
        "an over-cap user must raise AdmissionDenied(reason='cap_exceeded')"
    )

    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with db_session_factory() as session:
        outcomes = await list_for_tenant(session, tenant.id)
    assert len(outcomes) == 1
    assert outcomes[0].reason == "admission_cap_exceeded"
    assert outcomes[0].platform == platform
    assert outcomes[0].agent_id == "ag_1"
    assert outcomes[0].account_id is not None


async def _funded_tenant_with_spent_channel(session: AsyncSession) -> TenantRow:
    tenant = await make_tenant(session)
    await make_tenant_config(
        session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    await make_ledger_entry(
        session, tenant=tenant, delta_usd=Decimal("-1"), channel_id="chan-1", occurred_at=_NOW
    )
    return tenant


def _router_for(tenant: TenantRow) -> MARouter:
    return resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_admit_refuses_a_turn_in_a_channel_over_its_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
) -> None:
    tenant = await _funded_tenant_with_spent_channel(db_session)
    await make_channel_budget(db_session, tenant=tenant, platform=platform, limit_usd=Decimal("1"))
    await db_session.commit()
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_router_for(tenant)
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform=platform,
            external_user_id="user-1",
            channel_id="chan-1",
            thread_id="thread-1",
            now=_NOW,
        )
    assert exc_info.value.reason == "channel_budget_exceeded"

    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
    async with db_session_factory() as session:
        outcomes = await list_for_tenant(session, tenant.id)
    assert [o.reason for o in outcomes] == ["admission_channel_budget_exceeded"], (
        "a budget refusal is recorded as its own outcome"
    )


async def test_admit_attributes_the_channel_and_a_dm_to_its_source(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _funded_tenant_with_spent_channel(db_session)
    await db_session.commit()
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_router_for(tenant)
    )
    args = {"tenant_id": tenant.id, "platform": "discord", "external_user_id": "user-1"}

    unbudgeted = await admit(deps, **args, channel_id="chan-1", now=_NOW)
    assert unbudgeted.channel_id == "chan-1", "no budget row admits, and still attributes"

    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("0"))
    await db_session.commit()
    dm = await admit(deps, **args, channel_id="dm-chan", is_dm=True, now=_NOW)
    assert dm.channel_id is None, "a DM with no source channel is unattributed"
    moved = await admit(
        deps, **args, channel_id="dm-chan", is_dm=True, dm_source_channel_id="chan-2", now=_NOW
    )
    assert moved.channel_id == "chan-2", "a moved DM counts toward the channel it came from"
    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps, **args, channel_id="dm-chan", is_dm=True, dm_source_channel_id="chan-1", now=_NOW
        )
    assert exc_info.value.reason == "channel_budget_exceeded", "and is gated by its budget"


async def test_admit_gate_order_cap_wins_over_channel_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _funded_tenant_with_spent_channel(db_session)
    await make_tenant_user_cap(db_session, tenant=tenant, amount=Decimal("0"))
    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("0"))
    await db_session.commit()
    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=_router_for(tenant),
        billing_config=_billing_config(),
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )
    assert exc_info.value.reason == "cap_exceeded"


async def test_admit_missing_agent_only_raises_missing_turn_config_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(db_session, tenant=tenant, agent_name=None, environment_name="default")
    await db_session.commit()

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=MARouter(),
    )

    with pytest.raises(MissingTurnConfigError) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.missing == ("agent",), (
        "channel with only agent unresolved must report missing == ('agent',)"
    )


async def test_admit_missing_agent_and_environment_raises_missing_turn_config_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()  # no tenant config row at all -> both unresolved

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=MARouter(),
    )

    with pytest.raises(MissingTurnConfigError) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.missing == ("agent", "environment"), (
        "channel with both unresolved must report missing == ('agent', 'environment')"
    )


async def test_admit_unresolvable_tag_propagates_ma_resolver_miss_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A configured tag with no live MA resource and an empty defaults tree
    (apply_callable is a no-op) must raise MAResolverMissError unwrapped."""
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="ghost-agent", environment_name="ghost-env"
    )
    await db_session.commit()

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,  # empty tree: apply_callable is a real no-op
        router=router,
    )

    with pytest.raises(MAResolverMissError) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.kind == "agent", (
        "agent is resolved before environment; the miss must surface for 'agent' first"
    )


async def test_admit_happy_path_returns_admission_with_retrieved_agent_and_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()

    router = resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=router,
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        channel_id="chan-1",
        now=_NOW,
    )

    assert isinstance(admission, Admission), "admit() must return an Admission on the happy path"
    assert admission.agent.name == "daimon", "agent.name must come from a real agents.retrieve"
    assert admission.agent.model.id == "claude-sonnet-4-6", (
        "agent.model.id must come from a real agents.retrieve"
    )
    async with db_session_factory() as s:
        from daimon.core.stores.identity import get_or_create_platform_principal

        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant.id, platform="discord", external_id="user-1"
        )
    assert admission.account_id == principal.account_id, (
        "account_id must match the principal resolved for this (platform, external_id)"
    )


async def test_admit_gate_order_config_bail_wins_over_over_balance(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A tenant that is both over-balance AND mis-configured must see the
    config error -- config bail runs before the balance gate."""
    tenant = await make_tenant(db_session)
    # No tenant config row at all -> both agent/environment unresolved.
    # No ledger entry -> also over-balance.
    await db_session.commit()

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=MARouter(),
    )

    with pytest.raises(MissingTurnConfigError):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )


async def test_admit_raises_resolver_miss_when_the_resolved_agent_is_archived(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A resolved agent id that was live when cached but has since been
    archived out of band must raise MAResolverMissError -- the resolver's own
    liveness step never runs on this path (admission always passes
    cached_id=None), so a TTL-cache hit can hand back a dead id unchecked."""
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="doomed-agent",
        environment_name="default",
    )
    await db_session.commit()

    dead_agent = ma_agent(
        id="ag_dead",
        name="doomed-agent",
        tenant_id=tenant.id,
        archived_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    router = _archived_agent_router(tenant_id=tenant.id, dead_agent=dead_agent)
    cache = new_resolver_cache()
    cache[(tenant.id, "agent", "doomed-agent")] = "ag_dead"

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=router,
        resolver_cache=cache,
    )

    with pytest.raises(MAResolverMissError) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.kind == "agent", (
        "an archived agent must raise the agent-kind resolver miss"
    )
    assert exc_info.value.daimon_tag == "doomed-agent", (
        "the raised error must name the scoped agent tag the channel resolved to"
    )


async def test_admit_clears_the_scope_row_that_named_an_archived_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The self-heal clears every channel-scope row in the tenant naming the
    archived agent -- not just the channel that triggered this turn -- while
    leaving a row naming a different agent untouched."""
    tenant = await make_tenant(db_session)
    # The channel this turn resolves through: agent_name + environment_name.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="doomed-agent",
        environment_name="default",
    )
    # A second channel naming the same dead agent with no other field set --
    # clearing agent_name leaves nothing, so the row must auto-delete.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-2"),
        tenant_id=tenant.id,
        agent_name="doomed-agent",
    )
    # A third channel naming a different agent -- must survive the clear.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-3"),
        tenant_id=tenant.id,
        agent_name="other-agent",
    )
    await db_session.commit()

    dead_agent = ma_agent(
        id="ag_dead",
        name="doomed-agent",
        tenant_id=tenant.id,
        archived_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    router = _archived_agent_router(tenant_id=tenant.id, dead_agent=dead_agent)
    cache = new_resolver_cache()
    cache[(tenant.id, "agent", "doomed-agent")] = "ag_dead"

    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=router,
        resolver_cache=cache,
    )

    with pytest.raises(MAResolverMissError):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="chan-1",
            now=_NOW,
        )

    async with db_session_factory() as s:
        chan1 = await get_scope(s, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"))
        chan2 = await get_scope(s, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-2"))
        chan3 = await get_scope(s, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-3"))

    assert chan1 is not None and chan1.agent_name is None, (
        "the triggering channel's agent_name must be cleared; environment_name "
        "survives so the row itself is not deleted"
    )
    assert chan2 is None, (
        "a second channel naming the same dead agent with agent_name as its "
        "only field must be deleted outright once that field is cleared"
    )
    assert chan3 is not None and chan3.agent_name == "other-agent", (
        "a channel naming a different agent must be untouched by the tenant-wide clear"
    )


@pytest.mark.parametrize("role", ["admin", "user"])
async def test_role_is_persisted_even_when_configuration_blocks_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    role: str,
) -> None:
    from daimon.core.stores.accounts import get_account, set_role
    from daimon.core.stores.domain import Role
    from daimon.core.stores.identity import get_or_create_platform_principal

    tenant = await make_tenant(db_session, platform="slack")
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_CURRENT"
    )
    await set_role(db_session, principal.account_id, Role.USER if role == "admin" else Role.ADMIN)
    await db_session.commit()
    with pytest.raises(MissingTurnConfigError):
        await admit(
            _deps(sessionmaker=db_session_factory, router=MARouter(), defaults_root=tmp_path),
            tenant_id=tenant.id,
            platform="slack",
            external_user_id="U_CURRENT",
            channel_id="C_CHANNEL",
            thread_id="100.01",
            role=Role(role),
            now=_NOW,
        )
    db_session.expire_all()
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role == Role(role), (
        "successful role refresh precedes all turn gates"
    )


async def test_setup_admission_uses_bound_identity_and_leaves_missing_target_unchanged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="specialist", environment_name="default"
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="SETUP",
        responder_ma_agent_id="ag_exact",
        responder_name="daimon",
        configuration_target_ma_agent_id="ag_deleted",
        configuration_target_name="specialist",
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    responder = ma_agent(
        id="ag_exact",
        name="daimon",
        tenant_id=tenant.id,
        metadata={"daimon_managed": "true"},
        created_at=_NOW,
    )
    environment = ma_environment(
        id="env_science", name="default", tenant_id=tenant.id, created_at=_NOW.isoformat()
    )
    router = MARouter()
    router.add_agent(responder)
    router.add_environment_list(environment)
    router.add_environment(environment)
    admission = await admit(
        _deps(sessionmaker=db_session_factory, router=router, defaults_root=tmp_path),
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="MEMBER",
        channel_id="C_PARENT",
        thread_id="SETUP",
        now=_NOW,
    )
    assert admission.agent.id == "ag_exact", "setup never resolves the parent specialist"
    assert admission.config.configuration_target_ma_agent_id == "ag_deleted", (
        "missing target remains identifiable"
    )
    assert admission.environment.id == "env_science", "environment retains its existing resolution"


async def test_handoff_thread_admits_the_agent_the_task_was_handed_to(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A handed-over task answers as its new agent, not as Daimon and not as the
    channel default. The responder is resolved by concrete id, with no
    built-in-Daimon assertion — that assertion belongs to setup conversations,
    and applying it here would refuse every handoff."""
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="HANDED_OVER",
        responder_ma_agent_id="ag_stats",
        responder_name="stats-bot",
        kind="handoff",
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    stats = ma_agent(id="ag_stats", name="stats-bot", tenant_id=tenant.id)
    env = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent(stats)
    router.add_environment_list(env)
    router.add_environment(env)

    admission = await admit(
        _deps(sessionmaker=db_session_factory, router=router, defaults_root=tmp_path),
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="MEMBER",
        channel_id="C_PARENT",
        thread_id="HANDED_OVER",
        now=_NOW,
    )

    assert admission.agent.id == "ag_stats", "the agent the task was handed to answers here"
    assert admission.config.thread_binding_kind == "handoff", (
        "adapters need the kind to tell a handoff thread from a setup conversation"
    )
    assert admission.config.agent_name == "stats-bot", "the thread tier wins over the workspace"


async def test_handoff_thread_refuses_an_agent_from_another_workspace(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The concrete lookup is still tenant-checked: a handoff cannot reach across
    workspaces just because a thread row names an id."""
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="HANDED_OVER",
        responder_ma_agent_id="ag_foreign",
        responder_name="stats-bot",
        kind="handoff",
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    foreign = ma_agent(id="ag_foreign", name="stats-bot", tenant_id=uuid.uuid4())
    env = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent(foreign)
    router.add_environment_list(env)
    router.add_environment(env)

    with pytest.raises(DaimonError, match="no longer available in this workspace"):
        await admit(
            _deps(sessionmaker=db_session_factory, router=router, defaults_root=tmp_path),
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="MEMBER",
            channel_id="C_PARENT",
            thread_id="HANDED_OVER",
            now=_NOW,
        )


async def _seed_admittable_tenant(
    db_session: AsyncSession, *, policy: TenantAccessPolicy | None
) -> TenantRow:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    return tenant


def _admittable_router(tenant: TenantRow) -> MARouter:
    return resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )


async def test_admit_refuses_an_invoker_outside_the_allowlist_before_any_ma_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(invoker_user_ids=("staff-1",))
    )
    # An empty router fails any MA call, so a refusal here proves none was made.
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=MARouter())

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="guest-1",
            channel_id="chan-1",
            now=_NOW,
            role=Role.USER,
        )

    assert exc_info.value.reason == "invoker_not_allowed"


@pytest.mark.parametrize(
    ("policy", "external_user_id", "role"),
    [
        (None, "anyone", Role.USER),
        (TenantAccessPolicy(invoker_user_ids=("staff-1",)), "staff-1", Role.USER),
        (TenantAccessPolicy(invoker_user_ids=("staff-1",)), "owner-1", Role.ADMIN),
    ],
    ids=["no-policy-is-open", "allowlisted-user", "admin-not-listed"],
)
async def test_admit_admits_whoever_the_policy_allows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy | None,
    external_user_id: str,
    role: Role,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id=external_user_id,
        channel_id="chan-1",
        now=_NOW,
        role=role,
    )

    assert isinstance(admission, Admission)


async def test_admit_treats_a_missing_live_role_as_non_admin_even_for_a_stored_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A stored admin role may be stale (the user was demoted since), so only a
    live role the adapter passes can exempt a caller from the allowlist."""
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(invoker_user_ids=("staff-1",))
    )
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    # An earlier mention recorded the user as admin.
    await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="ex-admin",
        channel_id="chan-1",
        now=_NOW,
        role=Role.ADMIN,
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="ex-admin",
            channel_id="chan-1",
            now=_NOW,
        )

    assert exc_info.value.reason == "invoker_not_allowed", "no live role must mean non-admin"


async def test_admit_stores_the_live_platform_role_ids_like_the_role(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Role ids are replaced on each turn that passes them and kept when one does not."""
    from daimon.core.stores.accounts import get_account_with_tenant

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    async def turn(role_ids: list[str] | None) -> tuple[str, ...]:
        admission = await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="member-1",
            channel_id="chan-1",
            now=_NOW,
            platform_role_ids=role_ids,
        )
        async with db_session_factory() as session:
            row = await get_account_with_tenant(session, account_id=admission.account_id)
        assert row is not None, "the account exists"
        return row.platform_role_ids

    assert await turn(["r2", "r1"]) == ("r1", "r2"), "role ids are stored sorted"
    assert await turn(None) == ("r1", "r2"), "a turn with no role ids leaves them alone"
    assert await turn([]) == (), "a member who lost every role stores none"


async def test_admit_gate_order_invoker_refusal_wins_over_missing_config_and_balance(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A refused invoker must not learn the tenant is mis-configured or out of credit."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("staff-1",))
    )
    await db_session.commit()
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=MARouter())

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="slack",
            external_user_id="guest-1",
            channel_id="C1",
            now=_NOW,
        )

    assert exc_info.value.reason == "invoker_not_allowed"


@pytest.mark.parametrize("stored", ['{"bogus": true}', "null"], ids=["bad-object", "json-null"])
async def test_admit_refuses_when_the_stored_policy_is_unreadable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    stored: str,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": stored},
    )
    await db_session.commit()
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=MARouter())

    with pytest.raises(AccessPolicyUnreadable):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id="chan-1",
            now=_NOW,
            role=Role.ADMIN,
        )


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id", "is_dm", "expected"),
    [
        (None, "chan-1", None, False, False),
        (TenantAccessPolicy(sealed_channel_ids=("vault",)), "vault", None, False, True),
        (TenantAccessPolicy(sealed_channel_ids=("vault",)), "vault", "thr-1", False, True),
        (TenantAccessPolicy(sealed_channel_ids=("vault",)), "chan-1", "thr-1", False, False),
        (TenantAccessPolicy(dm_memory_read_only=True), "dm-1", None, True, True),
        (TenantAccessPolicy(dm_memory_read_only=True), "chan-1", None, False, False),
        (None, "dm-1", None, True, False),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1:1700000000.000100",)),
            "C1",
            "1700000000.000100",
            False,
            True,
        ),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1:1700000000.000100",)),
            "C1",
            "1700000000.000999",
            False,
            False,
        ),
    ],
    ids=[
        "open",
        "sealed-channel",
        "thread-under-sealed",
        "unsealed",
        "dm-read-only",
        "dm-flag-not-a-dm",
        "dm-default",
        "slack-thread-sealed-on-its-own",
        "slack-other-thread",
    ],
)
async def test_admit_marks_memory_read_only_for_sealed_channels_and_policy_dms(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy | None,
    channel_id: str,
    thread_id: str | None,
    is_dm: bool,
    expected: bool,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id=channel_id,
        thread_id=thread_id,
        now=_NOW,
        role=Role.USER,
        is_dm=is_dm,
    )

    assert admission.memory_read_only is expected, "memory_read_only must follow the policy"


@pytest.mark.parametrize(
    ("policy", "thread_id", "expected"),
    [
        (None, None, False),
        (TenantAccessPolicy(isolated_channel_ids=("chan-1",)), None, True),
        (TenantAccessPolicy(isolated_channel_ids=("chan-1",)), "thr-1", True),
        (TenantAccessPolicy(isolated_channel_ids=("other",)), None, False),
    ],
    ids=["open", "isolated-channel", "thread-under-isolated", "other-channel"],
)
async def test_admit_marks_isolated_turns_and_leaves_memory_writable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy | None,
    thread_id: str | None,
    expected: bool,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id="chan-1",
        thread_id=thread_id,
        now=_NOW,
        role=Role.USER,
    )

    assert admission.isolated is expected, "isolated must follow the policy"
    assert not admission.memory_read_only, "isolation keeps memory writable, unlike sealing"


async def test_start_dm_refuses_to_move_an_isolated_conversation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(isolated_channel_ids=("chan-1",))
    )
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id="chan-1",
        now=_NOW,
        role=Role.USER,
        is_dm=True,
    )

    with pytest.raises(DaimonError, match="isolated"):
        await start_dm(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            workspace_id="guild-1",
            route_key="dm-1",
            channel_id="dm-1",
            external_user_id="anyone",
            source_url="https://example.invalid/chan-1",
            source_channel_id="chan-1",
            context=[],
        )


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id", "category_id", "role"),
    [
        (TenantAccessPolicy(protected_channel_ids=("client",)), "client", None, None, Role.USER),
        (TenantAccessPolicy(protected_channel_ids=("client",)), "client", "thr-1", None, Role.USER),
        (TenantAccessPolicy(protected_category_ids=("cat-1",)), "chan-1", None, "cat-1", Role.USER),
        (TenantAccessPolicy(protected_channel_ids=("client",)), "client", None, None, Role.ADMIN),
    ],
    ids=["protected-channel", "thread-under-protected", "protected-category", "admin"],
)
async def test_admit_refuses_a_turn_whose_reply_would_land_in_a_protected_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy,
    channel_id: str,
    thread_id: str | None,
    category_id: str | None,
    role: Role,
) -> None:
    """SYS-048: the reply is an agent write too. Refused before the cascade, so
    the empty router proves no MA call, and before any thread or post exists."""
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=MARouter())

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id=channel_id,
            thread_id=thread_id,
            now=_NOW,
            role=role,
            category_id=category_id,
        )

    assert exc_info.value.reason == "channel_protected"


async def test_admit_still_admits_outside_the_protected_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(
        db_session,
        policy=TenantAccessPolicy(
            protected_channel_ids=("client",), protected_category_ids=("cat-1",)
        ),
    )
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id="chan-1",
        thread_id="thr-1",
        now=_NOW,
        role=Role.USER,
        category_id="cat-2",
    )

    assert isinstance(admission, Admission), "an unprotected channel must still be answered"


async def test_admit_protection_wins_over_the_invoker_refusal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The invoker refusal comes with a notice; in a protected channel the turn
    must be refused as protected so the adapters post nothing at all."""
    tenant = await _seed_admittable_tenant(
        db_session,
        policy=TenantAccessPolicy(invoker_user_ids=("staff",), protected_channel_ids=("client",)),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=MARouter())

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="guest",
            channel_id="client",
            now=_NOW,
            role=Role.USER,
        )

    assert exc_info.value.reason == "channel_protected"


@pytest.mark.parametrize(
    ("policy", "refused"),
    [
        (TenantAccessPolicy(protected_category_ids=("cat-1",)), True),
        (TenantAccessPolicy(protected_channel_ids=("elsewhere",)), False),
    ],
    ids=["categories-configured-fails-closed", "no-category-policy-admits"],
)
async def test_admit_with_an_unresolved_category(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy,
    refused: bool,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    call = admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id="chan-1",
        thread_id="thr-1",
        now=_NOW,
        role=Role.USER,
        category_unresolved=True,
    )

    if refused:
        with pytest.raises(AdmissionDenied) as exc_info:
            await call
        assert exc_info.value.reason == "channel_protected"
    else:
        assert isinstance(await call, Admission), "no category policy: nothing to check"


_PIN_DAIMON = TenantAccessPolicy(agent_channel_pins={"daimon": ("rx-chan",)})


@pytest.mark.parametrize(
    ("channel_id", "thread_id", "is_dm", "role"),
    [
        ("general", None, False, Role.USER),
        ("general", "thr-1", False, Role.USER),
        ("general", None, False, Role.ADMIN),
        ("rx-chan", "dm-scope", True, Role.USER),
    ],
    ids=["other-channel", "thread-under-other", "admin", "dm"],
)
async def test_admit_refuses_a_pinned_agent_outside_its_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    channel_id: str,
    thread_id: str | None,
    is_dm: bool,
    role: Role,
) -> None:
    """The pin holds however the turn got here, admins and DMs included."""
    tenant = await _seed_admittable_tenant(db_session, policy=_PIN_DAIMON)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id=channel_id,
            thread_id=thread_id,
            now=_NOW,
            role=role,
            is_dm=is_dm,
        )

    assert exc_info.value.reason == "agent_pinned_elsewhere"


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id"),
    [
        (_PIN_DAIMON, "rx-chan", None),
        (_PIN_DAIMON, "rx-chan", "thr-1"),
        (TenantAccessPolicy(agent_channel_pins={"daimon-rx": ("rx-chan",)}), "general", None),
    ],
    ids=["pinned-channel", "thread-under-pinned", "other-agent-pinned"],
)
async def test_admit_admits_a_pinned_agent_in_its_channels_and_leaves_others_alone(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy,
    channel_id: str,
    thread_id: str | None,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id=channel_id,
        thread_id=thread_id,
        now=_NOW,
        role=Role.USER,
    )

    assert isinstance(admission, Admission)

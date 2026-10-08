"""Unit tests for daimon.core.turn.admission.admit -- the D-01 stage-one
chokepoint: identity -> config cascade -> missing-config bail -> MA
resolve+retrieve -> balance gate -> cap gate.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.billing import BillingConfig
from daimon.core.channel_budget_notice import BudgetNotice, drain_budget_notices
from daimon.core.config import McpSettings
from daimon.core.direct_messages import start_dm
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import MAResolverMissError, ResolverCache, new_resolver_cache
from daimon.core.named_agent import bind_named_thread
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import AccessPolicyUnreadable, set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import FundingMode, Role, TenantRow
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.stores.thread_agent_bindings import create_binding, upsert_responder_binding
from daimon.core.turn.admission import (
    Admission,
    AdmissionDenied,
    ExternalFinding,
    MissingTurnConfigError,
    admit,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import NamedAgentRefused
from daimon.core.turn.termination import TerminationReason, termination_reason
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
    make_platform_principal,
    make_tenant,
    make_tenant_config,
    make_tenant_user_cap,
    make_thread_session,
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


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
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


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
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


async def test_a_budget_refusal_sends_the_notice_unless_the_tenant_opted_out(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _funded_tenant_with_spent_channel(db_session)
    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("1"))
    admin = await make_platform_principal(
        db_session, platform="discord", external_id="u-admin", tenant=tenant
    )
    await set_role(db_session, admin.account_id, Role.ADMIN)
    await db_session.commit()
    sent: list[BudgetNotice] = []
    release = asyncio.Event()

    async def notifier(notice: BudgetNotice) -> int:
        await release.wait()
        sent.append(notice)
        return 1

    base = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_router_for(tenant)
    )
    args = {"tenant_id": tenant.id, "platform": "discord", "external_user_id": "user-1"}
    opted_out = replace(base, budget_notifier=notifier, budget_notices_off=frozenset({tenant.id}))
    with pytest.raises(AdmissionDenied):
        await admit(opted_out, **args, channel_id="chan-1", now=_NOW)
    release.set()
    await drain_budget_notices()
    assert sent == [], "an opted-out tenant gets no notice"

    release.clear()
    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(replace(base, budget_notifier=notifier), **args, channel_id="chan-1", now=_NOW)
    assert exc_info.value.reason == "channel_budget_exceeded", "the notice leaves the refusal as is"
    assert sent == [], "the refusal returns while the notice is still being sent"
    release.set()
    await drain_budget_notices()
    assert [(n.channel_id, n.recipient_ids) for n in sent] == [("chan-1", ("u-admin",))]


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
    assert unbudgeted.budget_channel_id == "chan-1", "no budget row admits, and still attributes"

    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("0"))
    await db_session.commit()
    dm = await admit(deps, **args, channel_id="dm-chan", is_dm=True, now=_NOW)
    assert dm.budget_channel_id is None, "a DM with no source channel is unattributed"
    moved = await admit(
        deps, **args, channel_id="dm-chan", is_dm=True, dm_source_channel_id="chan-2", now=_NOW
    )
    assert moved.budget_channel_id == "chan-2", "a moved DM counts toward the channel it came from"
    assert moved.origin_channel_id == "dm-chan", "the seal still sees the DM, not its source"
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


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_named_agent_uses_the_normal_admission_and_refuses_a_thread_switch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    default = ma_agent(id="ag_default", name="daimon", tenant_id=tenant.id)
    named = ma_agent(id="ag_named", name="Planner", tenant_id=tenant.id)
    env = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent_list(default, named)
    router.add_agent(default)
    router.add_agent(named)
    router.add_environment_list(env)
    router.add_environment(env)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)
    args = dict(
        tenant_id=tenant.id,
        platform=platform,
        external_user_id="user-1",
        channel_id="channel",
        now=_NOW,
    )
    selected = await admit(deps, **args, requested_agent_name="ＰＬＡＮＮＥＲ")
    assert selected.agent.id == named.id
    assert selected.config.agent_name_tier == "named"
    assert (await admit(deps, **args, requested_agent_id=named.id)).agent.id == named.id
    assert (
        await admit(deps, **args, requested_agent_id=named.id, requested_agent_name="Planner")
    ).agent.id == named.id
    assert (await admit(deps, **args, requested_agent_name="unknown")).agent.id == default.id
    with pytest.raises(NamedAgentRefused) as two:
        await admit(
            deps,
            **args,
            requested_agent_ids=(default.id, named.id),
        )
    assert two.value.kind == "two"
    assert str(two.value) == ("You named two agents.\nUse one name, like `@daimon planner: …`")
    with pytest.raises(NamedAgentRefused, match="You named two agents"):
        await admit(
            deps,
            **args,
            requested_agent_id=default.id,
            requested_agent_name="Planner",
        )
    opening = await admit(deps, **args, thread_id="new-thread", requested_agent_name="Planner")
    assert opening.agent.id == named.id
    assert await bind_named_thread(
        db_session_factory,
        config=opening.config,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="new-thread",
        responder_ma_agent_id=opening.agent.id,
        responder_name=opening.agent.name,
        creator_account_id=opening.account_id,
    )
    assert not await bind_named_thread(
        db_session_factory,
        config=opening.config,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="new-thread",
        responder_ma_agent_id=default.id,
        responder_name=default.name,
        creator_account_id=opening.account_id,
    )
    follow_up = await admit(deps, **args, thread_id="new-thread")
    assert follow_up.agent.id == named.id
    assert follow_up.config.thread_binding_kind == "handoff"
    assert not await bind_named_thread(
        db_session_factory,
        config=follow_up.config,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="new-thread",
        responder_ma_agent_id=named.id,
        responder_name=named.name,
        creator_account_id=follow_up.account_id,
    )
    async with db_session_factory.begin() as session:
        await make_thread_session(
            session,
            tenant=tenant,
            platform=platform,
            thread_id="thread",
            ma_agent_id=default.id,
            channel_id="channel",
        )
    with pytest.raises(NamedAgentRefused) as thread_notice:
        await admit(deps, **args, thread_id="thread", requested_agent_name="Planner")
    assert thread_notice.value.kind == "thread"
    assert str(thread_notice.value) == (
        "This thread is with daimon.\nStart a new message in the channel to ask Planner."
    )
    assert thread_notice.value.hand_over_agent_id == named.id
    async with db_session_factory.begin() as session:
        await create_binding(
            session,
            tenant_id=tenant.id,
            platform=platform,
            parent_channel_id="channel",
            thread_id="setup-thread",
            responder_ma_agent_id=default.id,
            responder_name=default.name,
            kind="setup",
        )
    with pytest.raises(NamedAgentRefused) as setup_notice:
        await admit(deps, **args, thread_id="setup-thread", requested_agent_name="Planner")
    assert setup_notice.value.kind == "setup"
    assert str(setup_notice.value) == (
        "You're setting up daimon here.\nStart a new message in the channel to ask Planner."
    )
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                agent_rules={"Planner": AgentRule(runs_in=("another-channel",))}
            ),
        )
    with pytest.raises(NamedAgentRefused, match="That agent isn't available") as unavailable:
        await admit(deps, **args, requested_agent_name="Planner")
    assert unavailable.value.denial_reason == "runs_elsewhere"
    assert (
        termination_reason(unavailable.value) == TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE
    )


async def test_named_agent_in_an_own_readers_channel_names_its_own_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="private"),
        tenant_id=tenant.id,
        agent_name="local",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={"private": ChannelRule(readers="own", writers="own")},
            agent_rules={"local": AgentRule(runs_in=("private",))},
        ),
    )
    await db_session.commit()
    local = ma_agent(id="ag_local", name="local", tenant_id=tenant.id)
    visitor = ma_agent(id="ag_visitor", name="visitor", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent_list(local, visitor)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)
    with pytest.raises(NamedAgentRefused, match="Only local answers here"):
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="private",
            now=_NOW,
            requested_agent_name="visitor",
        )


async def test_hidden_agent_name_and_id_behave_like_unknown_outside_its_home(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={"home": ChannelRule(readers="own", writers="own")},
            agent_rules={"Planner": AgentRule(runs_in=("home",))},
        ),
    )
    await db_session.commit()
    default = ma_agent(id="ag_default", name="daimon", tenant_id=tenant.id)
    hidden = ma_agent(id="ag_hidden", name="Planner", tenant_id=tenant.id)
    env = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent_list(default, hidden)
    router.add_agent(default)
    router.add_agent(hidden)
    router.add_environment_list(env)
    router.add_environment(env)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)
    args = dict(
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        channel_id="elsewhere",
        now=_NOW,
    )

    ordinary = await admit(deps, **args)
    guessed = await admit(deps, **args, requested_agent_name="Planner")
    assert guessed.agent.id == ordinary.agent.id == default.id
    assert guessed.config.agent_name_tier == ordinary.config.agent_name_tier
    async with db_session_factory.begin() as session:
        await make_thread_session(
            session,
            tenant=tenant,
            platform="discord",
            thread_id="thread",
            ma_agent_id=default.id,
            channel_id="elsewhere",
        )
    assert (
        await admit(deps, **args, thread_id="thread", requested_agent_name="Planner")
    ).agent.id == default.id
    with pytest.raises(NamedAgentRefused, match="^That agent isn't available"):
        await admit(deps, **args, requested_agent_id=hidden.id)
    with pytest.raises(NamedAgentRefused) as hidden_and_visible:
        await admit(
            deps,
            **args,
            requested_agent_ids=(hidden.id, default.id),
        )
    assert hidden_and_visible.value.kind == "unavailable"
    assert (
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="home",
            now=_NOW,
            requested_agent_name="Planner",
        )
    ).agent.id == hidden.id


async def test_hidden_agent_does_not_create_a_name_collision_or_refusal_in_another_home(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="other"),
        tenant_id=tenant.id,
        agent_name="local",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={
                "home": ChannelRule(readers="own", writers="own"),
                "other": ChannelRule(readers="own", writers="own"),
            },
            agent_rules={
                "planner": AgentRule(runs_in=("home",)),
                "Secret": AgentRule(runs_in=("home",)),
                "local": AgentRule(runs_in=("other",)),
            },
        ),
    )
    await db_session.commit()
    default = ma_agent(id="ag_default", name="daimon", tenant_id=tenant.id)
    local = ma_agent(id="ag_local", name="local", tenant_id=tenant.id)
    hidden = ma_agent(id="ag_hidden", name="planner", tenant_id=tenant.id)
    other_hidden = ma_agent(id="ag_other_hidden", name="Secret", tenant_id=tenant.id)
    visible = ma_agent(id="ag_visible", name="Planner", tenant_id=tenant.id)
    env = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router = MARouter()
    router.add_agent_list(default, local, hidden, other_hidden, visible)
    router.add_agent(local)
    router.add_agent(visible)
    router.add_environment_list(env)
    router.add_environment(env)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)

    assert (
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="outside",
            now=_NOW,
            requested_agent_name="Planner",
        )
    ).agent.id == visible.id
    assert (
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            channel_id="other",
            now=_NOW,
            requested_agent_name="Secret",
        )
    ).agent.id == local.id


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
    assert (tenant.id, "agent", "doomed-agent") not in cache


async def test_admit_invalidates_archived_environment_cache(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="daimon",
        environment_name="archived-env",
    )
    await db_session.commit()
    agent = ma_agent(id="ag_live", name="daimon", tenant_id=tenant.id)
    environment = ma_environment(
        id="env_dead",
        name="archived-env",
        tenant_id=tenant.id,
        archived_at="2026-07-30T00:00:00Z",
    )
    router = MARouter()
    router.add_agent(agent)
    router.add_environment(environment)
    cache = new_resolver_cache()
    cache[(tenant.id, "agent", "daimon")] = agent.id
    cache[(tenant.id, "environment", "archived-env")] = environment.id
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

    assert exc_info.value.kind == "environment"
    assert (tenant.id, "environment", "archived-env") not in cache


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
    assert (admission.origin_channel_id, admission.origin_thread_id) == (channel_id, thread_id)
    # The seal stamp names every id that seals the turn, and follows the
    # channel seal only.
    expected_seals = (
        frozenset()
        if not expected or is_dm
        else frozenset(
            c
            for c in (channel_id, thread_id, f"{channel_id}:{thread_id}")
            if policy is not None and c in policy.channel_rules
        )
    )
    assert admission.origin_seal_ids == expected_seals


@pytest.mark.parametrize(
    ("sealed", "thread_id", "expected"),
    [
        (("vault", "thr-1"), "thr-1", {"vault", "thr-1"}),
        (("C1", "C1:1700000000.000100"), "1700000000.000100", {"C1", "C1:1700000000.000100"}),
    ],
    ids=["discord-parent-and-thread", "slack-parent-and-thread"],
)
async def test_admit_records_every_seal_on_a_thread_sealed_twice(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sealed: tuple[str, ...],
    thread_id: str,
    expected: set[str],
) -> None:
    """Unsealing the parent later must not drop the thread's own seal."""
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(sealed_channel_ids=sealed)
    )
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="anyone",
        channel_id=sealed[0],
        thread_id=thread_id,
        now=_NOW,
        role=Role.USER,
    )

    assert admission.origin_seal_ids == frozenset(expected)


_ISOLATED = TenantAccessPolicy(
    channel_rules={"chan-1": ChannelRule(readers="own", writers="own")},
    agent_rules={"own": AgentRule(runs_in=("chan-1",))},
)


@pytest.mark.parametrize(
    ("policy", "thread_id", "read_only"),
    [
        (None, None, False),
        (_ISOLATED, None, False),
        (_ISOLATED, "thr-1", False),
        (
            _ISOLATED.model_copy(
                update={"channel_rules": {"chan-1": ChannelRule(readers="inside")}}
            ),
            None,
            True,
        ),
    ],
    ids=["open", "isolated-channel", "thread-under-isolated", "sealed-only"],
)
async def test_admit_leaves_an_isolated_channels_own_agent_memory_writable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy | None,
    thread_id: str | None,
    read_only: bool,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    await _answer_in(db_session, tenant, "chan-1", "own")
    router = resolved_agent_env_router(
        ma_agent(id="ag_own", name="own", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)

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

    assert admission.memory_read_only is read_only, "a seal alone makes memory read-only"


async def _answer_in(db_session: AsyncSession, tenant: TenantRow, channel: str, agent: str) -> None:
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
        tenant_id=tenant.id,
        agent_name=agent,
        mode="agent",
    )
    await db_session.commit()


async def test_admit_refuses_an_agent_that_is_not_the_isolated_channels_own(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """With no agent of its own, the channel falls through to the workspace default,
    which answers elsewhere too: refused, not answered across the line."""
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id="chan-1",
            now=_NOW,
            role=Role.USER,
        )

    assert exc_info.value.reason == "own_agents_only"


_TEAMS_ROOM = "19:room@thread.tacv2"
_TEAMS_ISOLATED = TenantAccessPolicy(
    sealed_channel_ids=(_TEAMS_ROOM,),
    isolated_channel_ids=(_TEAMS_ROOM,),
    agent_channel_pins={"own": (_TEAMS_ROOM,)},
)


async def test_admit_holds_an_isolated_teams_channel_to_its_own_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Teams thread counts as its channel; its agent answers in no 1:1 chat."""
    tenant = await _seed_admittable_tenant(db_session, policy=_TEAMS_ISOLATED)
    await _answer_in(db_session, tenant, _TEAMS_ROOM, "own")
    await _answer_in(db_session, tenant, "a:chat-1", "own")
    router = resolved_agent_env_router(
        ma_agent(id="ag_own", name="own", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)
    args = {"tenant_id": tenant.id, "platform": "teams", "external_user_id": "anyone"}

    admission = await admit(
        deps,
        **args,
        channel_id=_TEAMS_ROOM,
        thread_id=f"{_TEAMS_ROOM};messageid=1700000000000",
        now=_NOW,
        role=Role.USER,
    )
    assert not admission.memory_read_only, "its own agent writes memory in the channel's threads"
    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            **args,
            channel_id="a:chat-1",
            thread_id="a:chat-1",
            now=_NOW,
            role=Role.USER,
            is_dm=True,
        )
    assert exc_info.value.reason == "runs_elsewhere", "the isolated agent never answers a 1:1 chat"


async def test_admit_refuses_an_outside_agent_in_an_isolated_teams_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_TEAMS_ISOLATED)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="teams",
            external_user_id="anyone",
            channel_id=_TEAMS_ROOM,
            thread_id=f"{_TEAMS_ROOM};messageid=1700000000000",
            now=_NOW,
            role=Role.USER,
        )

    assert exc_info.value.reason == "own_agents_only", "the workspace default stays outside"


async def test_admit_refuses_a_handoff_under_an_isolated_channel_to_an_outside_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    await _answer_in(db_session, tenant, "chan-2", "stats-bot")
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="chan-1",
        thread_id="HANDED_OVER",
        responder_ma_agent_id="ag_stats",
        responder_name="stats-bot",
        kind="handoff",
    )
    await db_session.commit()
    router = resolved_agent_env_router(
        ma_agent(id="ag_stats", name="stats-bot", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            _deps(sessionmaker=db_session_factory, router=router, defaults_root=tmp_path),
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="MEMBER",
            channel_id="chan-1",
            thread_id="HANDED_OVER",
            now=_NOW,
        )

    assert exc_info.value.reason == "own_agents_only"


async def test_admit_answers_a_setup_thread_under_an_isolated_channel_read_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Setup stays possible inside an isolated channel, but the built-in agent
    that runs it writes no memory there."""
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="chan-1",
        thread_id="SETUP",
        responder_ma_agent_id="ag_1",
        responder_name="daimon",
        configuration_target_ma_agent_id="ag_own",
        configuration_target_name="own",
    )
    await db_session.commit()
    router = resolved_agent_env_router(
        ma_agent(
            id="ag_1", name="daimon", tenant_id=tenant.id, metadata={"daimon_managed": "true"}
        ),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )

    admission = await admit(
        _deps(sessionmaker=db_session_factory, router=router, defaults_root=tmp_path),
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="MEMBER",
        channel_id="chan-1",
        thread_id="SETUP",
        now=_NOW,
    )

    assert admission.agent.id == "ag_1", "the setup conversation keeps its responder"
    assert admission.memory_read_only, "setup inside an isolated channel writes no memory"


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

    assert exc_info.value.reason == "writers_none"


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

    assert exc_info.value.reason == "writers_none"


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
        assert exc_info.value.reason == "writers_none"
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
    """The pin holds in every channel and thread, admins included, and for a member's DM."""
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

    assert exc_info.value.reason == "runs_elsewhere"


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


@pytest.mark.parametrize(
    ("policy", "channel_id", "thread_id", "is_dm", "expected"),
    [
        (TenantAccessPolicy(sealed_channel_ids=("C1",)), "C1", None, True, True),
        (TenantAccessPolicy(sealed_channel_ids=("C1",)), "C1", "T1", True, True),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1:1700000000.000100",)),
            "C1",
            "1700000000.000100",
            True,
            True,
        ),
        (TenantAccessPolicy(sealed_channel_ids=("C1",)), "C2", None, True, False),
        (TenantAccessPolicy(dm_memory_read_only=True), "C1", None, True, False),
    ],
    ids=["sealed", "thread-under-sealed", "slack-thread-sealed", "unsealed", "dm-read-only"],
)
async def test_admit_flags_a_sealed_source_apart_from_dm_memory_policy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    policy: TenantAccessPolicy,
    channel_id: str,
    thread_id: str | None,
    is_dm: bool,
    expected: bool,
) -> None:
    """/dm needs to know the source itself is sealed, not only that memory is read-only."""
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

    assert admission.source_sealed is expected


async def test_start_dm_refuses_a_sealed_source_admission() -> None:
    """Backstop for any /dm caller: no DM row is written for a sealed source."""
    from unittest.mock import MagicMock

    from daimon.core.direct_messages import SEALED_SOURCE_MESSAGE
    from daimon.core.errors import DaimonError
    from daimon.core.turn.admission import Admission

    admission = Admission(
        account_id=uuid.uuid4(),
        agent=MagicMock(),
        environment=MagicMock(),
        config=MagicMock(),
        source_sealed=True,
    )
    deps = MagicMock()
    with pytest.raises(DaimonError, match="Only turns inside this channel read it"):
        await start_dm(
            deps,
            admission,
            tenant_id=uuid.uuid4(),
            platform="discord",
            workspace_id="1",
            route_key="2",
            channel_id="2",
            external_user_id="3",
            source_url="https://example.invalid",
            source_channel_id="source",
            source_thread_id=None,
            context=[],
        )
    deps.sessionmaker.assert_not_called()
    assert "Only turns inside" in SEALED_SOURCE_MESSAGE


async def test_admit_refuses_a_handed_off_thread_whose_binding_names_the_agent_differently(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The binding recorded the agent under an older name; the pin is on the name
    the agent carries now. The metadata name must still catch it."""
    tenant = await _seed_admittable_tenant(db_session, policy=_PIN_DAIMON)
    await upsert_responder_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="general",
        thread_id="thr-handed",
        responder_ma_agent_id="ag_1",
        responder_name="renamed-bot",
        created_by_account_id=None,
        now=_NOW,
    )
    await db_session.commit()
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id="general",
            thread_id="thr-handed",
            now=_NOW,
            role=Role.USER,
        )

    assert exc_info.value.reason == "runs_elsewhere"


async def test_admit_refuses_an_agent_pinned_by_its_display_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The CLI and the write guard accept a pin on the MA display name; admission
    must honour the same name."""
    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(agent_channel_pins={"Daimon Display": ("rx",)})
    )
    router = resolved_agent_env_router(
        ma_agent(
            id="ag_1",
            name="Daimon Display",
            tenant_id=tenant.id,
            metadata={"daimon_name": "daimon"},
        ),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)

    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(
            deps,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="anyone",
            channel_id="general",
            now=_NOW,
            role=Role.USER,
        )
    assert exc_info.value.reason == "runs_elsewhere"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_admit_exempts_an_admin_from_a_pin_in_a_dm_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
) -> None:
    """Admins are trusted: a DM reaches only them, so the pin doesn't apply there.

    On Teams the adapter passes ADMIN for the deployment's ``admin_user_ids``
    in a personal chat, so the same rule covers them.
    """
    tenant = await _seed_admittable_tenant(db_session, policy=_PIN_DAIMON)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )

    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform=platform,  # pyright: ignore[reportArgumentType]
        external_user_id="anyone",
        channel_id="general",
        thread_id="dm-scope",
        now=_NOW,
        role=Role.ADMIN,
        is_dm=True,
    )

    assert isinstance(admission, Admission)
    # Every DM-admitted session is stamped private (Teams personal chats too),
    # so no admin reads it from the hub.
    assert admission.private_dm_id == "dm-scope"


async def test_an_admins_dm_with_an_isolated_channels_agent_is_held_to_its_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An admin may DM chan-1's own agent; the turn counts toward chan-1 and stops at its budget."""
    policy = _ISOLATED.model_copy(
        update={"agent_rules": {"daimon": AgentRule(runs_in=("chan-1",))}}
    )
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    args = {
        "tenant_id": tenant.id,
        "platform": "discord",
        "external_user_id": "boss",
        "channel_id": "dm-1",
        "is_dm": True,
        "role": Role.ADMIN,
    }

    admission = await admit(deps, **args, now=_NOW)
    assert admission.budget_channel_id == "chan-1", "charged to the agent's channel"

    await make_channel_budget(db_session, tenant=tenant, limit_usd=Decimal("0"))
    await db_session.commit()
    with pytest.raises(AdmissionDenied) as exc_info:
        await admit(deps, **args, now=_NOW)
    assert exc_info.value.reason == "channel_budget_exceeded", "a $0 channel budget stops it"


async def _admit_as(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant: TenantRow,
    *,
    channel_id: str = "chan-1",
    thread_id: str | None = None,
    is_external: bool | None,
    is_known: bool = True,
    is_dm: bool = False,
    role: Role = Role.USER,
    platform_role_ids: list[str] | None = None,
) -> Admission:
    router = resolved_agent_env_router(
        ma_agent(id="ag_own", name="own", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)
    return await admit(
        deps,
        tenant_id=tenant.id,
        platform="teams",
        external_user_id="guest-1",
        channel_id=channel_id,
        thread_id=thread_id,
        now=_NOW,
        role=role,
        is_dm=is_dm,
        external=None if is_external is None else ExternalFinding(is_external, is_known),
        platform_role_ids=platform_role_ids,
    )


async def _stored(db_session: AsyncSession, admission: Admission) -> tuple[bool, str]:
    row = await db_session.execute(
        text("SELECT is_external, role FROM accounts WHERE id = :id"),
        {"id": admission.account_id},
    )
    is_external, role = row.one()
    return is_external, role


@pytest.mark.parametrize("thread_id", [None, "thr-1"], ids=["channel", "thread"])
async def test_an_external_participant_is_answered_inside_an_isolated_channel_as_a_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    thread_id: str | None,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    admission = await _admit_as(
        db_session_factory, tmp_path, tenant, thread_id=thread_id, is_external=True, role=Role.ADMIN
    )
    assert admission.is_external and admission.grant.is_external
    assert not admission.grant.subject.is_admin, "an admin role is ignored"
    assert await _stored(db_session, admission) == (True, Role.USER.value), "stored, demoted"


@pytest.mark.parametrize(
    ("channel_id", "is_dm"),
    [("chan-2", False), ("dm-1", True)],
    ids=["open-channel", "dm"],
)
async def test_an_external_participant_is_refused_outside_an_isolated_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    channel_id: str,
    is_dm: bool,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    with pytest.raises(AdmissionDenied) as exc_info:
        await _admit_as(
            db_session_factory,
            tmp_path,
            tenant,
            channel_id=channel_id,
            is_dm=is_dm,
            is_external=True,
        )
    assert exc_info.value.reason == "external_participant"


async def test_a_continuation_that_cannot_tell_keeps_the_stored_external_flag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    await _admit_as(db_session_factory, tmp_path, tenant, is_external=True)
    with pytest.raises(AdmissionDenied) as exc_info:
        await _admit_as(db_session_factory, tmp_path, tenant, channel_id="chan-2", is_external=None)
    assert exc_info.value.reason == "external_participant", "still external"


async def test_an_external_participant_is_refused_in_an_isolated_channels_setup_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The built-in agent answers a setup thread, with wider reads than the channel's own."""
    from daimon.core.stores.thread_agent_bindings import create_binding

    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        parent_channel_id="chan-1",
        thread_id="setup-1",
        responder_ma_agent_id="ag_daimon",
        responder_name="daimon",
        kind="setup",
    )
    await db_session.commit()
    with pytest.raises(AdmissionDenied) as exc_info:
        await _admit_as(db_session_factory, tmp_path, tenant, thread_id="setup-1", is_external=True)
    assert exc_info.value.reason == "external_participant", "refused before the setup agent"


async def test_a_finding_without_evidence_holds_for_the_turn_and_stores_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A fail-closed guess makes this turn an external's, but never demotes the account."""
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    first = await _admit_as(
        db_session_factory, tmp_path, tenant, is_external=False, role=Role.ADMIN
    )
    guessed = await _admit_as(
        db_session_factory,
        tmp_path,
        tenant,
        is_external=True,
        is_known=False,
        role=Role.ADMIN,
        platform_role_ids=["team-1"],
    )
    assert guessed.is_external and not guessed.grant.subject.is_admin, "external this turn"
    assert await _stored(db_session, first) == (False, Role.ADMIN.value), "nothing stored"
    with pytest.raises(AdmissionDenied) as exc_info:
        await _admit_as(
            db_session_factory,
            tmp_path,
            tenant,
            channel_id="chan-2",
            is_external=True,
            is_known=False,
        )
    assert exc_info.value.reason == "external_participant", "refused outside isolation"


async def test_only_positive_internal_evidence_clears_a_stored_external_flag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    marked = await _admit_as(db_session_factory, tmp_path, tenant, is_external=True)
    unsure = await _admit_as(
        db_session_factory, tmp_path, tenant, is_external=False, is_known=False
    )
    assert unsure.is_external, "an unsure internal guess does not outweigh stored evidence"
    assert await _stored(db_session, marked) == (True, Role.USER.value)
    cleared = await _admit_as(db_session_factory, tmp_path, tenant, is_external=False)
    assert not cleared.is_external
    assert await _stored(db_session, marked) == (False, Role.USER.value), "cleared, still user"


async def test_an_internal_caller_is_admitted_as_before_and_may_be_an_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    admission = await _admit_as(
        db_session_factory, tmp_path, tenant, is_external=False, role=Role.ADMIN
    )
    assert not admission.is_external and admission.grant.subject.is_admin
    assert await _stored(db_session, admission) == (False, Role.ADMIN.value)


async def test_an_external_participant_is_never_a_channel_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Neither a grant naming them nor one naming a team they own admits them."""
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.channel_admins import set_channel_admins

    tenant = await _seed_admittable_tenant(db_session, policy=_ISOLATED)
    await _answer_in(db_session, tenant, "chan-1", "own")
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="chan-1",
        role_ids=["team-1"],
        user_ids=["guest-1"],
        actor_account_id=None,
    )
    await db_session.commit()
    admission = await _admit_as(
        db_session_factory, tmp_path, tenant, is_external=True, platform_role_ids=["team-1"]
    )
    assert admission.grant.subject.administered_channel_ids == frozenset(), "administers nothing"
    async with db_session_factory() as session:
        row = await get_account_with_tenant(session, account_id=admission.account_id)
    assert row is not None and row.platform_role_ids == (), "no team is stored for MCP calls"

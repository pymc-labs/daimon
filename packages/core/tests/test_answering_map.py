"""Tests for the answering map: the pure fold and its two-read shell.

The pure cases build rows directly; the shell case writes real config rows and
a real setup binding through the stores and reads them back over Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.answering_map import (
    AnsweringMap,
    ChannelAnswer,
    TenantAnswer,
    build_answering_map,
    load_answering_map,
    routed_agent_names,
)
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
)
from daimon.core.stores import scoped_config_write
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_account, make_tenant, make_tenant_config
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)


def _setup_binding(*, thread_id: str, target_name: str | None) -> ThreadAgentBindingRow:
    tenant_id = uuid.uuid4()
    return ThreadAgentBindingRow(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id=thread_id,
        kind="setup",
        responder_ma_agent_id="ag_daimon",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=target_name,
        creator_account_id=None,
        archived=False,
        locked=False,
        deleted=False,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_build_answering_map_orders_channel_overrides_and_carries_their_audit() -> None:
    """Channel overrides read in channel_id order, each with who set it and when."""
    tenant_id = uuid.uuid4()
    setter = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tenant_id, channel_id="c2", agent_name="beta", mode="agent"),
        ChannelConfigRow(
            tenant_id=tenant_id,
            channel_id="c1",
            agent_name="alpha",
            mode="agent",
            agent_name_set_by_account_id=setter,
            agent_name_set_at=_NOW,
        ),
    ]
    answering = build_answering_map(
        tenant=None,
        channels=channels,
        default=DeploymentDefault(agent_name="daimon"),
        setup_threads=[],
        setup_threads_truncated=False,
    )
    assert [row.channel_id for row in answering.channel_overrides] == ["c1", "c2"], (
        "channel overrides must come out ordered by channel_id"
    )
    assert answering.channel_overrides[0].set_by_account_id == setter, (
        "the override must carry who set it"
    )
    assert answering.channel_overrides[0].set_at == _NOW, "the override must carry when it was set"


def test_own_channels_only_map_omits_ignored_defaults() -> None:
    tenant_id = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tenant_id, channel_id="shared", agent_name="old"),
        ChannelConfigRow(tenant_id=tenant_id, channel_id="private", agent_name="own"),
    ]
    answering = build_answering_map(
        tenant=None,
        channels=channels,
        default=DeploymentDefault(agent_name="daimon", channel_defaults="own_channels_only"),
        setup_threads=[],
        setup_threads_truncated=False,
        own_reader_channel_ids=("private",),
    )
    assert [(row.channel_id, row.agent_name) for row in answering.channel_overrides] == [
        ("private", "own")
    ]


def test_build_answering_map_omits_user_active_rows_at_both_tiers() -> None:
    """A scope handed to a user-active session routes to nobody, so it is not an override."""
    tenant_id = uuid.uuid4()
    answering = build_answering_map(
        tenant=TenantConfigRow(tenant_id=tenant_id, agent_name="alpha", mode="user_active"),
        channels=[
            ChannelConfigRow(
                tenant_id=tenant_id, channel_id="c1", agent_name="beta", mode="user_active"
            )
        ],
        default=DeploymentDefault(agent_name="daimon"),
        setup_threads=[],
        setup_threads_truncated=False,
    )
    assert answering.channel_overrides == (), "a user_active channel is not a channel override"
    assert answering.tenant_default is None, "a user_active tenant row is not a workspace default"
    assert not answering.tenant_consumes_fallthrough, (
        "a user_active tenant row leaves the deployment fall-through in place"
    )


def test_build_answering_map_reports_the_tenant_default_consuming_the_fallthrough() -> None:
    """A workspace default removes the deployment default from the cascade."""
    tenant_id = uuid.uuid4()
    answering = build_answering_map(
        tenant=TenantConfigRow(tenant_id=tenant_id, agent_name="alpha", mode="agent"),
        channels=[],
        default=DeploymentDefault(agent_name="daimon"),
        setup_threads=[],
        setup_threads_truncated=False,
    )
    assert answering.tenant_default is not None, "an agent-mode tenant row is the workspace default"
    assert answering.tenant_default.agent_name == "alpha", "the workspace default is named"
    assert answering.deployment_default == "daimon", (
        "the deployment default is still reported, so a renderer can say what it is"
    )
    assert answering.tenant_consumes_fallthrough, (
        "a workspace default consumes the deployment fall-through for the whole install"
    )


def test_build_answering_map_carries_setup_threads_and_their_truncation_flag() -> None:
    """Live setup conversations come through with what each one is setting up."""
    answering = build_answering_map(
        tenant=None,
        channels=[],
        default=DeploymentDefault(),
        setup_threads=[
            _setup_binding(thread_id="t1", target_name="alpha"),
            _setup_binding(thread_id="t2", target_name=None),
        ],
        setup_threads_truncated=True,
    )
    assert [row.thread_id for row in answering.setup_threads] == ["t1", "t2"], (
        "setup threads keep the order the store read handed over"
    )
    assert answering.setup_threads[0].target_name == "alpha", (
        "a setup conversation with a chosen target must say which agent it is setting up"
    )
    assert answering.setup_threads[1].target_name is None, (
        "a setup conversation with no target yet must not invent one"
    )
    assert answering.setup_threads_truncated, "the truncation flag passes through unchanged"


async def test_load_answering_map_folds_real_config_rows_and_setup_conversations(
    db_session: AsyncSession,
) -> None:
    """The shell reads both halves for one install and hands back one map."""
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="workspace-agent", actor_account_id=actor.id
    )
    await scoped_config_write.set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="channel-agent",
        actor_account_id=actor.id,
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c1",
        thread_id="setup-thread",
        responder_ma_agent_id="ag_daimon",
        responder_name="daimon",
        configuration_target_ma_agent_id="ag_alpha",
        configuration_target_name="alpha",
        creator_account_id=actor.id,
    )

    answering = await load_answering_map(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        default=DeploymentDefault(agent_name="daimon"),
    )

    assert [(row.channel_id, row.agent_name) for row in answering.channel_overrides] == [
        ("c1", "channel-agent")
    ], "the channel row written through the store must come back as a channel override"
    assert answering.channel_overrides[0].set_by_account_id == actor.id, (
        "the stored attribution must survive the read"
    )
    assert answering.tenant_default is not None, "the tenant row must come back as the default"
    assert answering.tenant_default.agent_name == "workspace-agent", (
        "the workspace default must be the agent the tenant row names"
    )
    assert [row.thread_id for row in answering.setup_threads] == ["setup-thread"], (
        "the live setup conversation must be listed"
    )
    assert answering.setup_threads[0].target_name == "alpha", (
        "the setup conversation must carry the agent it is setting up"
    )
    assert not answering.setup_threads_truncated, "one conversation is not a truncated listing"


def test_build_answering_map_lays_out_environments_beside_the_agents() -> None:
    """A channel naming only an environment is no agent override, whatever its mode."""
    tenant_id = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tenant_id, channel_id="c2", environment_name="science"),
        ChannelConfigRow(
            tenant_id=tenant_id,
            channel_id="c1",
            environment_name="gpu",
            mode="user_active",
        ),
        ChannelConfigRow(tenant_id=tenant_id, channel_id="c3", agent_name="alpha"),
    ]

    answering = build_answering_map(
        tenant=TenantConfigRow(tenant_id=tenant_id, environment_name="shared"),
        channels=channels,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
        setup_threads=[],
        setup_threads_truncated=False,
    )

    assert [(row.channel_id, row.environment_name) for row in answering.channel_environments] == [
        ("c1", "gpu"),
        ("c2", "science"),
    ], "channels with their own environment come out in channel_id order, mode ignored"
    assert [row.channel_id for row in answering.channel_overrides] == ["c3"], (
        "an environment-only row must not read as routing to an agent"
    )
    assert answering.environment_in("c2") == ("science", "channel"), "own setting wins"
    assert answering.environment_in("c3") == ("shared", "tenant"), "then the workspace default"
    assert answering.environment_in(None) == ("shared", "tenant"), "no channel reads the tenant"


def test_answering_map_environment_falls_through_to_the_deployment_default() -> None:
    answering = build_answering_map(
        tenant=None,
        channels=[],
        default=DeploymentDefault(environment_name="default"),
        setup_threads=[],
        setup_threads_truncated=False,
    )

    assert answering.tenant_environment is None, "no tenant row means no workspace environment"
    assert answering.environment_in("c1") == ("default", "deployment"), (
        "with no row at all the deployment default decides, exactly as before"
    )
    assert build_answering_map(
        tenant=None,
        channels=[],
        default=DeploymentDefault(),
        setup_threads=[],
        setup_threads_truncated=False,
    ).environment_in("c1") == (None, None), "nothing configured resolves to nothing"


def test_routed_agent_names_drops_the_deployment_default_behind_a_workspace_default() -> None:
    """A workspace default takes the fall-through, so the deployment default reaches nobody."""
    overrides = (ChannelAnswer(channel_id="c1", agent_name="alpha"),)
    with_tenant = AnsweringMap(
        channel_overrides=overrides,
        tenant_default=TenantAnswer(agent_name="beta"),
        deployment_default="daimon",
        tenant_consumes_fallthrough=True,
    )
    without_tenant = AnsweringMap(channel_overrides=overrides, deployment_default="daimon")
    assert routed_agent_names(with_tenant) == {"alpha", "beta"}, "deployment default is shadowed"
    assert routed_agent_names(without_tenant) == {"alpha", "daimon"}, "it answers with no tenant"


async def test_resolve_own_channels_only_keeps_own_default_and_ignores_other_rules(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await scoped_config_write.set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="shared"),
        tenant_id=tenant.id,
        agent_name="old",
    )
    await scoped_config_write.set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="private"),
        tenant_id=tenant.id,
        agent_name="own",
    )
    await scoped_config_write.set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="inside"),
        tenant_id=tenant.id,
        agent_name="inside-agent",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={
                "private": ChannelRule(readers="own", writers="none"),
                "inside": ChannelRule(readers="inside", writers="any"),
            }
        ),
    )
    default = DeploymentDefault(agent_name="daimon", channel_defaults="own_channels_only")
    shared = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant.id, channel_id="shared"),
        default=default,
    )
    private = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant.id, channel_id="private"),
        default=default,
    )
    inside = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant.id, channel_id="inside"),
        default=default,
    )
    assert (shared.agent_name, shared.agent_name_tier) == ("daimon", "deployment")
    assert (private.agent_name, private.agent_name_tier) == ("own", "channel")
    assert (inside.agent_name, inside.agent_name_tier) == ("daimon", "deployment")
    rollback = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant.id, channel_id="shared"),
        default=default.model_copy(update={"channel_defaults": "legacy"}),
    )
    assert (rollback.agent_name, rollback.agent_name_tier) == ("old", "channel")

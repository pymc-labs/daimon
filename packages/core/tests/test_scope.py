"""Pure unit tests for daimon.core.scope merge/_pick_* and ScopeContext validation.

These tests import DeploymentDefault, TenantConfigRow, ChannelConfigRow, pick_agent,
_pick_environment, and merge from daimon.core.scope using the new 3-tier signature
(channel, tenant, default). They are RED until the scope rewrite lands.
"""

from __future__ import annotations

import uuid

from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    answering_places,
    is_agent_reachable,
    merge,
    pick_agent,
)

# ---------------------------------------------------------------------------
# merge — 3-tier cascade (channel → tenant → deployment)
# ---------------------------------------------------------------------------


def test_merge_deployment_tier_wins_when_no_rows() -> None:
    """The behavior-preserved spine invariant: no rows → deployment default resolves."""
    result = merge(
        channel=None,
        tenant=None,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "daimon", (
        "merge with no channel/tenant rows must resolve agent_name from the injected default"
    )
    assert result.agent_name_tier == "deployment", (
        "tier must be 'deployment' when the injected default is the only source"
    )
    assert result.environment_name == "default", (
        "merge with no channel/tenant rows must resolve environment_name from the injected default"
    )
    assert result.environment_name_tier == "deployment", (
        "tier must be 'deployment' for environment_name too when only default is set"
    )


def test_merge_tenant_tier_wins() -> None:
    """A tenant config row beats the deployment default."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="custom", environment_name="prod")
    result = merge(
        channel=None,
        tenant=tenant,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "custom", "tenant row must beat the deployment default"
    assert result.agent_name_tier == "tenant", "tier must be 'tenant' when tenant row wins"
    assert result.environment_name == "prod", "tenant env must beat the deployment default"
    assert result.environment_name_tier == "tenant", "env tier must be 'tenant'"


def test_merge_channel_tier_wins() -> None:
    """A channel config row beats both tenant and deployment default."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="chan")
    tenant = TenantConfigRow(tenant_id=tid, agent_name="custom")
    result = merge(
        channel=channel,
        tenant=tenant,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "chan", "channel row must beat tenant and deployment default"
    assert result.agent_name_tier == "channel", "tier must be 'channel' when channel row wins"


def test_merge_returns_all_none_when_no_rows_and_empty_default() -> None:
    """All tiers absent or None → all result fields are None."""
    result = merge(channel=None, tenant=None, default=DeploymentDefault())
    assert result.agent_name is None, "all-None input should yield None agent_name"
    assert result.agent_name_tier is None, "all-None input should yield None tier"
    assert result.environment_name is None, "all-None input should yield None environment_name"
    assert result.environment_name_tier is None, "all-None input should yield None tier"


def test_merge_partial_rows_fill_field_by_field() -> None:
    """agent_name from channel, environment_name from deployment default."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(
        tenant_id=tid, channel_id="c1", agent_name="chan", environment_name=None
    )
    result = merge(
        channel=channel,
        tenant=None,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "chan", "channel provides agent_name"
    assert result.agent_name_tier == "channel", "tier should be channel for agent_name"
    assert result.environment_name == "default", (
        "deployment default provides environment_name when channel has none"
    )
    assert result.environment_name_tier == "deployment", (
        "env tier should be deployment when channel has no environment_name"
    )


def test_merge_is_pure_same_inputs_same_output() -> None:
    """Pure function: same inputs must produce identical output."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(
        tenant_id=tid, channel_id="c1", agent_name="a", environment_name=None
    )
    default = DeploymentDefault(agent_name="b", environment_name="c")
    r1 = merge(channel=channel, tenant=None, default=default)
    r2 = merge(channel=channel, tenant=None, default=default)
    assert r1 == r2, "pure function: same inputs must produce identical output"


def test_merge_channel_wins_over_tenant_and_default() -> None:
    """channel beats tenant + default on both fields."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(
        tenant_id=tid, channel_id="c1", agent_name="ch-agent", environment_name="ch-env"
    )
    tenant = TenantConfigRow(tenant_id=tid, agent_name="ws-agent", environment_name="ws-env")
    result = merge(
        channel=channel,
        tenant=tenant,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    assert result.agent_name == "ch-agent", "channel wins over tenant and deployment"
    assert result.agent_name_tier == "channel"
    assert result.environment_name == "ch-env"
    assert result.environment_name_tier == "channel"


# ---------------------------------------------------------------------------
# pick_agent — mode gate
# ---------------------------------------------------------------------------


def test_pick_agent_skips_user_active() -> None:
    """A TenantConfigRow with mode='user_active' is skipped; falls through to default."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="x", mode="user_active")
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    name, tier = pick_agent(channel=None, tenant=tenant, default=default)
    assert name == "daimon", (
        "mode='user_active' tenant row must be skipped; default should provide the agent name"
    )
    assert tier == "deployment", (
        "tier should be 'deployment' when user_active row is skipped and default provides the value"
    )


def test_pick_agent_channel_user_active_skips_to_tenant() -> None:
    """A channel row with mode='user_active' is skipped; tenant row wins."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="ch", mode="user_active")
    tenant = TenantConfigRow(tenant_id=tid, agent_name="tenant-bot", mode="agent")
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    name, tier = pick_agent(channel=channel, tenant=tenant, default=default)
    assert name == "tenant-bot", (
        "user_active channel must be skipped; tenant row provides the agent name"
    )
    assert tier == "tenant"


def test_pick_agent_returns_none_when_all_user_active_and_no_default() -> None:
    """All rows user_active and no deployment default → returns (None, None)."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="ch", mode="user_active")
    tenant = TenantConfigRow(tenant_id=tid, agent_name="t", mode="user_active")
    default = DeploymentDefault()
    name, tier = pick_agent(channel=channel, tenant=tenant, default=default)
    assert name is None, "no resolvable agent when all rows user_active and no deployment default"
    assert tier is None


# ---------------------------------------------------------------------------
# _pick_environment — mode is ignored
# ---------------------------------------------------------------------------


def test_pick_environment_ignores_mode() -> None:
    """A channel row with mode='user_active' STILL contributes environment_name."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(
        tenant_id=tid, channel_id="c1", environment_name="dev-env", mode="user_active"
    )
    default = DeploymentDefault(environment_name="default")
    result = merge(
        channel=channel,
        tenant=None,
        default=default,
    )
    assert result.environment_name == "dev-env", (
        "environment_name resolution is independent of mode; "
        "user_active channel should still contribute"
    )
    assert result.environment_name_tier == "channel", (
        "env tier should be 'channel' even when mode='user_active'"
    )


def test_pick_environment_tenant_user_active_contributes_env() -> None:
    """A tenant row with mode='user_active' still provides environment_name."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, environment_name="prod", mode="user_active")
    result = merge(
        channel=None,
        tenant=tenant,
        default=DeploymentDefault(environment_name="default"),
    )
    assert result.environment_name == "prod", (
        "tenant row with user_active mode must still contribute environment_name"
    )
    assert result.environment_name_tier == "tenant"


# ---------------------------------------------------------------------------
# ChannelConfigRow and TenantConfigRow defaults
# ---------------------------------------------------------------------------


def test_channel_config_row_has_mode_field_defaulting_to_agent() -> None:
    tid = uuid.uuid4()
    row = ChannelConfigRow(tenant_id=tid, channel_id="c1")
    assert row.mode == "agent", "ChannelConfigRow.mode should default to 'agent'"


def test_tenant_config_row_has_mode_field_defaulting_to_agent() -> None:
    tid = uuid.uuid4()
    row = TenantConfigRow(tenant_id=tid)
    assert row.mode == "agent", "TenantConfigRow.mode should default to 'agent'"


def test_channel_config_row_accepts_user_active_mode() -> None:
    tid = uuid.uuid4()
    row = ChannelConfigRow(tenant_id=tid, channel_id="c1", mode="user_active")
    assert row.mode == "user_active", "ChannelConfigRow should accept mode='user_active'"


def test_tenant_config_row_accepts_user_active_mode() -> None:
    tid = uuid.uuid4()
    row = TenantConfigRow(tenant_id=tid, mode="user_active")
    assert row.mode == "user_active", "TenantConfigRow should accept mode='user_active'"


# ---------------------------------------------------------------------------
# ScopeContext — new shape (tenant_id + optional channel_id + optional account_id)
# ---------------------------------------------------------------------------


def test_scope_context_requires_only_tenant_id() -> None:
    """ScopeContext only requires tenant_id after the collapse."""
    ctx = ScopeContext(tenant_id=uuid.uuid4())
    assert ctx.channel_id is None
    assert ctx.account_id is None


def test_scope_context_accepts_channel_id() -> None:
    ctx = ScopeContext(tenant_id=uuid.uuid4(), channel_id="c1")
    assert ctx.channel_id == "c1"


def test_scope_context_accepts_account_id() -> None:
    aid = uuid.uuid4()
    ctx = ScopeContext(tenant_id=uuid.uuid4(), account_id=aid)
    assert ctx.account_id == aid


def test_scope_context_accepts_all_fields() -> None:
    tid = uuid.uuid4()
    aid = uuid.uuid4()
    ctx = ScopeContext(tenant_id=tid, channel_id="c1", account_id=aid)
    assert ctx.tenant_id == tid
    assert ctx.channel_id == "c1"
    assert ctx.account_id == aid


# ---------------------------------------------------------------------------
# is_agent_reachable — reachability predicate over channel/tenant/deployment
# ---------------------------------------------------------------------------


def test_is_agent_reachable_true_for_deployment_default_on_fresh_install() -> None:
    """No tenant row, no channel rows: the seeded deployment default is reachable."""
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    assert is_agent_reachable("daimon", tenant=None, channels=[], default=default), (
        "the deployment-default agent must be reachable on a fresh install with no config rows"
    )


def test_is_agent_reachable_false_for_unrelated_name_on_fresh_install() -> None:
    """No tenant row, no channel rows: an agent nobody named is not reachable."""
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    assert not is_agent_reachable("scratch", tenant=None, channels=[], default=default), (
        "an agent that is neither scoped nor the deployment default must be unreachable"
    )


def test_is_agent_reachable_true_when_tenant_row_names_it() -> None:
    """A tenant row in mode='agent' naming the agent makes it reachable."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="agent")
    assert is_agent_reachable("alpha", tenant=tenant, channels=[], default=DeploymentDefault()), (
        "a tenant row naming the agent in mode='agent' must make it reachable"
    )


def test_is_agent_reachable_false_for_deployment_default_when_tenant_overrides() -> None:
    """A tenant row that names a different agent consumes the fall-through."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="agent")
    default = DeploymentDefault(agent_name="daimon")
    assert not is_agent_reachable("daimon", tenant=tenant, channels=[], default=default), (
        "a tenant row overriding the agent must consume the deployment fall-through"
    )


def test_is_agent_reachable_true_for_default_when_tenant_row_is_user_active() -> None:
    """A tenant row in mode='user_active' does not consume the fall-through."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="user_active")
    default = DeploymentDefault(agent_name="daimon")
    assert is_agent_reachable("daimon", tenant=tenant, channels=[], default=default), (
        "a non-agent-mode tenant row must not consume the deployment fall-through"
    )


def test_is_agent_reachable_true_for_default_when_tenant_agent_name_empty() -> None:
    """A tenant row in mode='agent' with no agent_name does not consume the fall-through."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name=None, mode="agent")
    default = DeploymentDefault(agent_name="daimon")
    assert is_agent_reachable("daimon", tenant=tenant, channels=[], default=default), (
        "an empty agent_name on an agent-mode tenant row must not consume the fall-through, "
        "matching pick_agent's truthiness test"
    )


def test_is_agent_reachable_true_when_channel_row_names_it() -> None:
    """A channel row in mode='agent' naming the agent makes it reachable."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="beta", mode="agent")
    assert is_agent_reachable(
        "beta", tenant=None, channels=[channel], default=DeploymentDefault()
    ), "a channel row naming the agent in mode='agent' must make it reachable"


def test_is_agent_reachable_true_for_default_alongside_a_channel_row() -> None:
    """A channel row scoping one agent does not suppress the deployment default for others."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="beta", mode="agent")
    default = DeploymentDefault(agent_name="daimon")
    assert is_agent_reachable("daimon", tenant=None, channels=[channel], default=default), (
        "a channel row cannot suppress the deployment tier for other channels"
    )


def test_is_agent_reachable_false_when_channel_row_is_user_active() -> None:
    """A channel row in mode='user_active' does not make its agent_name reachable."""
    tid = uuid.uuid4()
    channel = ChannelConfigRow(
        tenant_id=tid, channel_id="c1", agent_name="beta", mode="user_active"
    )
    assert not is_agent_reachable(
        "beta", tenant=None, channels=[channel], default=DeploymentDefault()
    ), "a user_active channel row must not make its agent_name reachable"


def test_is_agent_reachable_false_when_no_default_and_no_rows() -> None:
    """No deployment default and no rows at all: nothing is reachable."""
    assert not is_agent_reachable(
        "anything", tenant=None, channels=[], default=DeploymentDefault()
    ), "with no default and no rows, no agent name should be reachable"


def test_is_agent_reachable_is_case_sensitive() -> None:
    """Matching is exact and case-sensitive."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="agent")
    assert not is_agent_reachable(
        "Alpha", tenant=tenant, channels=[], default=DeploymentDefault()
    ), "matching must be case-sensitive: 'Alpha' must not match a row named 'alpha'"


# ---------------------------------------------------------------------------
# answering_places — the itemised form of is_agent_reachable
# ---------------------------------------------------------------------------


def _row_combinations() -> list[tuple[TenantConfigRow | None, list[ChannelConfigRow]]]:
    """Every tenant-row shape crossed with every channel-row shape that matters."""
    tid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    tenants: list[TenantConfigRow | None] = [
        None,
        TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="agent"),
        TenantConfigRow(tenant_id=tid, agent_name="other", mode="agent"),
        TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="user_active"),
        TenantConfigRow(tenant_id=tid, agent_name=None, mode="agent"),
    ]
    channel_sets: list[list[ChannelConfigRow]] = [
        [],
        [ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="alpha", mode="agent")],
        [ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="other", mode="agent")],
        [ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="alpha", mode="user_active")],
        [
            ChannelConfigRow(tenant_id=tid, channel_id="c2", agent_name="alpha", mode="agent"),
            ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="alpha", mode="agent"),
        ],
    ]
    return [(tenant, channels) for tenant in tenants for channels in channel_sets]


def test_answering_places_truthiness_matches_is_agent_reachable_over_every_row_combination() -> (
    None
):
    """Empty result means unreachable and a non-empty one means reachable, always.

    Two functions answering the same question are free to disagree the moment
    one of them grows a case the other lacks; this pins them together across
    the tenant/channel/default shapes that decide the answer.
    """
    for default in (DeploymentDefault(), DeploymentDefault(agent_name="alpha")):
        for tenant, channels in _row_combinations():
            places = answering_places("alpha", tenant=tenant, channels=channels, default=default)
            reachable = is_agent_reachable(
                "alpha", tenant=tenant, channels=channels, default=default
            )
            assert bool(places) == reachable, (
                f"answering_places and is_agent_reachable disagree for tenant={tenant!r}, "
                f"channels={channels!r}, default={default!r}"
            )


def test_answering_places_orders_channels_then_tenant_then_deployment() -> None:
    """The result reads in precedence order, with channels sorted by channel_id."""
    tid = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tid, channel_id="c2", agent_name="alpha", mode="agent"),
        ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="alpha", mode="agent"),
    ]
    tenant = TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="agent")
    places = answering_places(
        "alpha", tenant=tenant, channels=channels, default=DeploymentDefault(agent_name="alpha")
    )
    assert places == (
        AnsweringPlace(tier="channel", channel_id="c1"),
        AnsweringPlace(tier="channel", channel_id="c2"),
        AnsweringPlace(tier="tenant", channel_id=None),
    ), (
        "channels must come first sorted by channel_id, then the tenant tier; the deployment "
        "tier must be absent because the tenant row consumes the fall-through"
    )


def test_answering_places_reports_the_deployment_tier_when_nothing_overrides_it() -> None:
    """A fresh install routes to the deployment default, with no channel attached."""
    places = answering_places(
        "daimon", tenant=None, channels=[], default=DeploymentDefault(agent_name="daimon")
    )
    assert places == (AnsweringPlace(tier="deployment", channel_id=None),), (
        "the deployment fall-through must be reported as its own place with no channel_id"
    )


def test_answering_places_omits_user_active_rows() -> None:
    """A channel handed to a user-active session is not a place the agent answers."""
    tid = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tid, channel_id="c1", agent_name="alpha", mode="user_active"),
        ChannelConfigRow(tenant_id=tid, channel_id="c2", agent_name="alpha", mode="agent"),
    ]
    places = answering_places(
        "alpha",
        tenant=TenantConfigRow(tenant_id=tid, agent_name="alpha", mode="user_active"),
        channels=channels,
        default=DeploymentDefault(),
    )
    assert places == (AnsweringPlace(tier="channel", channel_id="c2"),), (
        "user_active rows must be omitted at both the channel and the tenant tier"
    )

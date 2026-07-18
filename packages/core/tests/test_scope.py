"""Pure unit tests for daimon.core.scope merge/_pick_* and ScopeContext validation.

These tests import DeploymentDefault, TenantConfigRow, ChannelConfigRow, _pick_agent,
_pick_environment, and merge from daimon.core.scope using the new 3-tier signature
(channel, tenant, default). They are RED until the scope rewrite lands.
"""

from __future__ import annotations

import uuid

from daimon.core.scope import (
    ChannelConfigRow,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    _pick_agent,  # pyright: ignore[reportPrivateUsage]  # test seam
    merge,
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
# _pick_agent — mode gate
# ---------------------------------------------------------------------------


def test_pick_agent_skips_user_active() -> None:
    """A TenantConfigRow with mode='user_active' is skipped; falls through to default."""
    tid = uuid.uuid4()
    tenant = TenantConfigRow(tenant_id=tid, agent_name="x", mode="user_active")
    default = DeploymentDefault(agent_name="daimon", environment_name="default")
    name, tier = _pick_agent(channel=None, tenant=tenant, default=default)
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
    name, tier = _pick_agent(channel=channel, tenant=tenant, default=default)
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
    name, tier = _pick_agent(channel=channel, tenant=tenant, default=default)
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

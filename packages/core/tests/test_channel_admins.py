"""Pure channel admin rules and the agent reach they are checked against."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.agent_reach import build_agent_reach
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    InvalidChannelAdminIds,
    administered_channel_ids,
    is_channel_admin,
    normalize_channel_admin_ids,
)
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.domain import ChannelAdminsRow

TENANT = uuid.uuid4()
SNOWFLAKE = "123456789012345678"


def _grant(channel_id: str, *, roles: tuple[str, ...] = (), users: tuple[str, ...] = ()):
    return ChannelAdminsRow(
        tenant_id=TENANT,
        platform="discord",
        channel_id=channel_id,
        role_ids=roles,
        user_ids=users,
        updated_by_account_id=None,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_is_channel_admin_by_server_admin_user_or_role() -> None:
    grant = _grant("c1", roles=("r1",), users=("u1",))
    assert is_channel_admin(ChannelAdminCaller(platform_user_id="u1"), grant=grant)
    assert is_channel_admin(
        ChannelAdminCaller(platform_user_id="u2", role_ids=frozenset({"r1"})), grant=grant
    )
    assert is_channel_admin(
        ChannelAdminCaller(platform_user_id="u2", is_server_admin=True), grant=None
    )
    assert not is_channel_admin(ChannelAdminCaller(platform_user_id="u2"), grant=grant)
    assert not is_channel_admin(ChannelAdminCaller(platform_user_id="u1"), grant=None)


def test_administered_channel_ids_lists_only_named_grants() -> None:
    grants = [_grant("c1", users=("u1",)), _grant("c2", roles=("r1",)), _grant("c3")]
    caller = ChannelAdminCaller(platform_user_id="u1", role_ids=frozenset({"r1"}))
    assert administered_channel_ids(caller, grants) == {"c1", "c2"}
    server_admin = ChannelAdminCaller(platform_user_id="u9", is_server_admin=True)
    assert administered_channel_ids(server_admin, grants) == set(), "needs no grant"


def test_normalize_ids_checks_platform_formats() -> None:
    assert normalize_channel_admin_ids(
        "discord", channel_id=f" {SNOWFLAKE} ", role_ids=[SNOWFLAKE, SNOWFLAKE], user_ids=[]
    ) == (SNOWFLAKE, (SNOWFLAKE,), ())
    assert normalize_channel_admin_ids(
        "slack", channel_id="C0123", role_ids=[], user_ids=["U0456"]
    ) == ("C0123", (), ("U0456",))
    for platform, kwargs in (
        ("discord", {"channel_id": "general", "role_ids": [], "user_ids": []}),
        ("discord", {"channel_id": SNOWFLAKE, "role_ids": ["<@&1>"], "user_ids": []}),
        ("slack", {"channel_id": "C0123", "role_ids": ["S1"], "user_ids": []}),
        ("slack", {"channel_id": "C0123", "role_ids": [], "user_ids": ["C0123"]}),
        ("cli", {"channel_id": "c", "role_ids": [], "user_ids": []}),
        (
            "discord",
            {
                "channel_id": SNOWFLAKE,
                "role_ids": [],
                "user_ids": [str(10**17 + n) for n in range(26)],
            },
        ),
    ):
        with pytest.raises(InvalidChannelAdminIds):
            normalize_channel_admin_ids(platform, **kwargs)


def _channel(channel_id: str, agent: str) -> ChannelConfigRow:
    return ChannelConfigRow(tenant_id=TENANT, channel_id=channel_id, agent_name=agent)


def test_agent_reach_is_local_only_inside_the_given_channels() -> None:
    default = DeploymentDefault(agent_name="daimon")
    reach = build_agent_reach(
        "helper",
        tenant=None,
        channels=[_channel("c1", "helper"), _channel("c2", "other")],
        default=default,
        thread_parent_channel_ids=["c3"],
    )
    assert reach.channel_ids == {"c1", "c3"}, "a bound thread counts as its parent channel"
    assert reach.is_local_to({"c1", "c3", "c9"})
    assert not reach.is_local_to({"c1"})

    tenant = TenantConfigRow(tenant_id=TENANT, agent_name="helper")
    wide = build_agent_reach("helper", tenant=tenant, channels=[], default=default)
    assert wide.is_tenant_wide and not wide.is_local_to({"c1"})
    fallthrough = build_agent_reach("daimon", tenant=None, channels=[], default=default)
    assert not fallthrough.is_local_to({"c1"}), "the deployment default is tenant-wide"

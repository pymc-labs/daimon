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
    fit_lines,
    fold_mentions,
    is_channel_admin,
    normalize_channel_admin_ids,
)
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.direct_messages import DmOrigin
from daimon.core.stores.domain import ChannelAdminsRow
from daimon.core.stores.routines import RoutineCreator

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
    assert is_channel_admin(ChannelAdminCaller(platform_user_id="u1"), grant=grant), "by user"
    assert is_channel_admin(
        ChannelAdminCaller(platform_user_id="u2", role_ids=frozenset({"r1"})), grant=grant
    ), "by role"
    assert is_channel_admin(
        ChannelAdminCaller(platform_user_id="u2", is_server_admin=True), grant=None
    ), "a server admin needs no grant"
    assert not is_channel_admin(ChannelAdminCaller(platform_user_id="u2"), grant=grant), (
        "an unnamed member is not a channel admin"
    )
    assert not is_channel_admin(ChannelAdminCaller(platform_user_id="u1"), grant=None), (
        "no grant, no channel admin"
    )


def test_administered_channel_ids_lists_only_named_grants() -> None:
    grants = [_grant("c1", users=("u1",)), _grant("c2", roles=("r1",)), _grant("c3")]
    caller = ChannelAdminCaller(platform_user_id="u1", role_ids=frozenset({"r1"}))
    assert administered_channel_ids(caller, grants) == {"c1", "c2"}, "user and role grants"
    server_admin = ChannelAdminCaller(platform_user_id="u9", is_server_admin=True)
    assert administered_channel_ids(server_admin, grants) == set(), "needs no grant"


def test_normalize_ids_checks_platform_formats() -> None:
    assert normalize_channel_admin_ids(
        "discord", channel_id=f" {SNOWFLAKE} ", role_ids=[SNOWFLAKE, SNOWFLAKE], user_ids=[]
    ) == (SNOWFLAKE, (SNOWFLAKE,), ()), "stripped and de-duplicated"
    assert normalize_channel_admin_ids(
        "slack", channel_id="C0123", role_ids=[], user_ids=["U0456"]
    ) == ("C0123", (), ("U0456",)), "a Slack channel and user"
    for platform, kwargs in (
        ("discord", {"channel_id": "general", "role_ids": [], "user_ids": []}),
        ("discord", {"channel_id": SNOWFLAKE, "role_ids": ["<@&1>"], "user_ids": []}),
        ("slack", {"channel_id": "C0123", "role_ids": ["S1"], "user_ids": []}),
        ("slack", {"channel_id": "C0123", "role_ids": [], "user_ids": ["C0123"]}),
        ("slack", {"channel_id": "D0123", "role_ids": [], "user_ids": ["U0456"]}),
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


def test_listing_helpers_fold_mentions_and_stop_at_the_character_budget() -> None:
    assert fold_mentions(["a", "b"]) == "a, b", "few mentions are listed in full"
    assert fold_mentions([str(n) for n in range(8)]) == "0, 1, 2, 3, 4 +3 more", "the rest fold"
    assert fit_lines(["aaa", "bbb", "ccc"], max_chars=7) == ["aaa", "bbb"], (
        "newlines count toward the budget"
    )
    assert fit_lines(["x" * 10, "y"], max_chars=5) == [], "an oversized first line stops it"


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
    assert reach.is_local_to({"c1", "c3", "c9"}, platform_user_id="u1"), "inside the channels"
    assert not reach.is_local_to({"c1"}, platform_user_id="u1"), "c3 is outside"

    tenant = TenantConfigRow(tenant_id=TENANT, agent_name="helper")
    wide = build_agent_reach("helper", tenant=tenant, channels=[], default=default)
    assert wide.is_tenant_wide, "the tenant default is tenant-wide"
    assert not wide.is_local_to({"c1"}, platform_user_id="u1"), "tenant-wide is never local"
    fallthrough = build_agent_reach("daimon", tenant=None, channels=[], default=default)
    assert not fallthrough.is_local_to({"c1"}, platform_user_id="u1"), (
        "the deployment default is tenant-wide"
    )


def test_only_a_stronger_creators_routine_makes_an_agent_not_local() -> None:
    default = DeploymentDefault(agent_name="daimon")
    grants = [_grant("c1", users=("u1", "co")), _grant("c2", roles=("r9",))]

    def local(creator: RoutineCreator) -> bool:
        reach = build_agent_reach(
            "helper",
            tenant=None,
            channels=[_channel("c1", "helper")],
            default=default,
            routine_creators=[creator],
            grants=grants,
        )
        return reach.is_local_to({"c1"}, platform_user_id="u1")

    assert local(RoutineCreator(platform_user_id="u1")), "the caller's own routine"
    assert local(RoutineCreator(platform_user_id="member")), "a plain member's gains nothing"
    assert local(RoutineCreator(platform_user_id="co")), "a co-admin of c1 holds no more"
    assert not local(RoutineCreator(platform_user_id="boss", is_admin=True)), (
        "a server admin's routine would run the caller's edits with admin rights"
    )
    assert not local(RoutineCreator(platform_user_id="other", role_ids=("r9",))), (
        "so would one by c2's admin, granted by role"
    )


def test_agent_reach_counts_a_dm_as_the_channel_it_started_from() -> None:
    default = DeploymentDefault(agent_name="daimon")
    live = DmOrigin(channel_id="dm1", scope_id="dm:live", source_channel_id="c1")
    legacy = DmOrigin(channel_id="dm2", scope_id="dm:legacy", source_channel_id=None)
    reach = build_agent_reach(
        "helper",
        tenant=None,
        channels=[_channel("dm1", "helper"), _channel("dm2", "helper"), _channel("dm3", "helper")],
        default=default,
        dm_origins=[live, legacy],
        dm_bindings=[
            ("dm1", "dm:live", "helper"),
            ("dm1", "dm:old", "helper"),
            ("dm3", "dm:gone", "helper"),
            ("dm2", "dm:legacy", "other"),
        ],
    )
    assert reach.channel_ids == {"c1", "dm2"}, (
        "a live DM counts as its source channel, one with no recorded source as itself, "
        "and a replaced or moved-away DM scope not at all"
    )
    assert not reach.is_local_to({"c1"}, platform_user_id="u1"), "dm2's source is unknown"

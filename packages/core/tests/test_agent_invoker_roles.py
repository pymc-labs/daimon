"""Per-agent human role gates cannot be bypassed by another execution surface."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_rules import keep_to_own, release_agents_of
from daimon.core.permissions import agent_permissions, with_agent_rule
from pydantic import ValidationError

GATED = TenantAccessPolicy(agent_rules={"restricted": AgentRule(invoker_role_ids=("123",))})
MEMBER = Subject(platform_user_id="human", verified_membership=True, role_ids=("123",))


@pytest.mark.parametrize("surface", list(Surface))
@pytest.mark.parametrize("admin", [False, True])
def test_agent_role_gate_precedes_every_surface_admin_exemption(
    surface: Surface, admin: bool
) -> None:
    subject = Subject(platform_user_id="human", is_admin=admin, verified_membership=True)
    decision = authorize(
        GATED,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=surface,
        agent=AgentRef.of("restricted"),
    )
    assert decision.reason == "invoker_not_allowed"


@pytest.mark.parametrize("membership", [None, False])
def test_unverified_role_claim_is_not_membership(membership: bool | None) -> None:
    subject = Subject(platform_user_id="human", role_ids=("123",), verified_membership=membership)
    assert not authorize(
        GATED, subject=subject, action=Action.RUN_AGENT, agent=AgentRef.of("restricted")
    )


def test_live_member_and_tenant_allowlist_are_both_required():
    assert authorize(
        GATED, subject=MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("restricted")
    )
    tenant_restricted = TenantAccessPolicy(
        invoker_user_ids=("other",),
        agent_rules=GATED.agent_rules,
    )
    assert not authorize(
        tenant_restricted, subject=MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("restricted")
    )
    assert authorize(
        tenant_restricted,
        subject=Subject(is_admin=True, platform_user_id="admin"),
        action=Action.RUN_AGENT,
        agent=AgentRef.of("ordinary"),
    )
    assert authorize(
        tenant_restricted,
        subject=Subject(is_admin=True, platform_user_id="admin"),
        action=Action.START_TURN,
    )


def test_handoff_requires_target_role_even_for_channel_default_and_admin():
    for subject in (
        Subject(platform_user_id="human"),
        Subject(is_admin=True, platform_user_id="admin", verified_membership=True),
    ):
        assert (
            authorize(
                GATED,
                subject=subject,
                action=Action.HAND_OFF,
                agent=AgentRef.of("restricted"),
                answers_here=True,
                place=Place(channel_id="channel"),
            ).reason
            == "invoker_not_allowed"
        )
    assert authorize(
        GATED,
        subject=MEMBER,
        action=Action.HAND_OFF,
        agent=AgentRef.of("restricted"),
        answers_here=True,
    )


def test_routines_and_nonhuman_bearers_cannot_use_the_role_gate():
    assert not authorize(
        GATED,
        subject=MEMBER,
        action=Action.RUN_AGENT,
        agent=AgentRef.of("restricted"),
        surface=Surface.ROUTINE,
    )
    assert not authorize(
        GATED,
        subject=Subject(is_admin=True, role_ids=("123",), verified_membership=True),
        action=Action.RUN_AGENT,
        agent=AgentRef.of("restricted"),
    )


def test_aliases_require_each_restriction_and_role_only_does_not_pin_channels():
    policy = TenantAccessPolicy(
        agent_rules={
            **GATED.agent_rules,
            "display": AgentRule(invoker_role_ids=("456",)),
        }
    )
    assert not authorize(
        policy, subject=MEMBER, action=Action.RUN_AGENT, agent=AgentRef.of("display", "restricted")
    )
    assert agent_permissions(policy, ("restricted",)).runs_in == ()


def test_legacy_rows_and_role_only_roundtrip():
    legacy = TenantAccessPolicy.model_validate({"agent_channel_pins": {"old": ["channel"]}})
    assert legacy.agent_rules["old"] == AgentRule(runs_in=("channel",))
    assert TenantAccessPolicy.model_validate({"agent_rules": {"old": {"runs_in": []}}})
    assert TenantAccessPolicy.model_validate_json(GATED.model_dump_json()) == GATED
    assert (
        with_agent_rule(TenantAccessPolicy(), "restricted", GATED.agent_rules["restricted"])
        == GATED
    )
    assert "invoker_role_ids" not in legacy.model_dump()["agent_rules"]["old"]


@pytest.mark.parametrize("role", ["0", "01", "-1", "role", str(2**64)])
def test_agent_role_ids_validate_like_tenant_roles(role: str) -> None:
    with pytest.raises(ValidationError):
        AgentRule(invoker_role_ids=(role,))


def test_spatial_rule_editors_preserve_invoker_roles():
    own = keep_to_own(
        GATED,
        channel_id="channel",
        rule=ChannelRule(readers="own", writers="own"),
        agent_name="restricted",
    )
    assert own.agent_rules["restricted"].invoker_role_ids == ("123",)
    released, names = release_agents_of(own, "channel")
    assert names == ("restricted",)
    assert released.agent_rules == GATED.agent_rules


@pytest.mark.parametrize("platform", ["slack", "teams", "cli", "discord"])
@pytest.mark.parametrize("bot", [False, True])
async def test_role_membership_is_human_and_discord_only(platform: str, bot: bool) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

    from daimon.core.membership import CachedMembershipResolver, Membership
    from daimon.core.membership_subject import resolve_subject

    tenant_id = uuid4()
    tenant = SimpleNamespace(platform=platform, external_id="111", archived_at=None)
    fetch = AsyncMock(
        return_value=Membership(
            is_member=True,
            role_ids=("123",),
            is_admin=True,
            is_bot=bot,
        )
    )
    resolver = CachedMembershipResolver(fetch)
    session = AsyncMock()
    with (
        patch("daimon.core.membership_subject.get_tenant", AsyncMock(return_value=tenant)),
        patch("daimon.core.membership_subject.is_daimon_admin", AsyncMock(return_value=True)),
    ):
        subject = await resolve_subject(
            MagicMock(),
            resolver,
            tenant_id=tenant_id,
            user_id="human",
            session=session,
            require_fresh=True,
        )
    decision = authorize(
        GATED, subject=subject, action=Action.RUN_AGENT, agent=AgentRef.of("restricted")
    )
    assert bool(decision) is (platform == "discord" and not bot)
    if platform == "discord":
        fetch.assert_awaited_once_with(tenant_id, "111", "human")
    else:
        fetch.assert_not_awaited()


@pytest.mark.parametrize("automated", ["dm_routine", "wake"])
def test_recorded_human_owner_does_not_authorize_automated_invocation(automated: str) -> None:
    from uuid import uuid4

    from daimon.core.turn.admission import (
        AdmissionDenied,
        AdmissionGrant,
        _require_run_agent,  # pyright: ignore[reportPrivateUsage]
    )

    grant = AdmissionGrant(
        tenant_id=uuid4(),
        subject=MEMBER,
        surface=Surface.DM,
        turn_place=Place(),
        run_place=Place(),
        agent=AgentRef.of("restricted"),
        channel_id="dm",
        thread_id=None,
        is_dm=True,
        act_for_creator=automated == "dm_routine",
        human_invocation=automated != "wake",
    )
    with pytest.raises(AdmissionDenied, match="invoker_not_allowed"):
        _require_run_agent(GATED, grant)
    _require_run_agent(TenantAccessPolicy(), grant)


def test_role_gated_agent_refuses_routine_save_even_for_live_admin() -> None:
    from dataclasses import replace

    assert (
        authorize(
            GATED,
            subject=replace(MEMBER, is_admin=True),
            action=Action.SAVE_ROUTINE,
            agent=AgentRef.of("restricted"),
        ).reason
        == "invoker_not_allowed"
    )
    assert authorize(
        GATED,
        subject=MEMBER,
        action=Action.SAVE_ROUTINE,
        agent=AgentRef.of("ordinary"),
    )


@pytest.mark.parametrize(
    "origin,attended,expected",
    [
        ("chat", None, True),
        ("chat", True, True),
        ("chat", False, False),
        ("handoff", True, True),
        ("task_handoff", True, True),
        ("task_handoff", False, False),
        ("timer", True, False),
        ("watch_fired", True, False),
        ("job_finished", True, False),
        ("worker_finished", True, False),
        ("routine", True, False),
        ("long_job", True, False),
        ("cli", True, False),
        ("headless", True, False),
        ("future_origin", True, False),
        ("worker", False, False),
    ],
)
def test_human_principal_origins_fail_closed(
    origin: str, attended: bool | None, expected: bool
) -> None:
    from daimon.core.turn.invocation import human_invocation

    assert human_invocation(origin, attended) is expected


@pytest.mark.parametrize("root_human", [False, True])
def test_workers_inherit_frozen_principal_independently_of_attendance(root_human: bool) -> None:
    from uuid import uuid4

    from daimon.core.turn.admission import AdmissionGrant
    from daimon.core.turn.invocation import human_invocation

    grant = AdmissionGrant(
        tenant_id=uuid4(),
        subject=MEMBER,
        surface=Surface.CHANNEL,
        turn_place=Place(),
        run_place=Place(),
        agent=AgentRef.of("restricted"),
        channel_id="channel",
        thread_id="thread",
        is_dm=False,
        human_invocation=root_human,
    )
    assert human_invocation("worker", False, grant) is root_human
    assert human_invocation("worker", True, grant) is root_human
    assert not human_invocation("unknown", True, grant)

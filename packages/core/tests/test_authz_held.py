"""Held turns and client requesters in `authorize`: refused before every exemption.

A turn is held to its origin channel (`TurnHold`) when the requester is from
another organisation or the channel is shared. These rows prove the refusals
come first: before an admin exemption and before the no-rules ALLOW returns.
"""

from __future__ import annotations

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    Decision,
    Place,
    SessionFacts,
    Surface,
    authorize,
    build_subject,
)
from daimon.core.turn_hold import TurnHold

OPEN = TenantAccessPolicy()
RULED = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)})
MULTI = TenantAccessPolicy(agent_channel_pins={"multi": ("C_SHARED", "C_OPS")})
POLICIES = [pytest.param(OPEN, id="open"), pytest.param(RULED, id="rules")]

CLIENT = build_subject(is_admin=False, platform_user_id="U_EXTERNAL", client=True)
MEMBER = build_subject(is_admin=False, platform_user_id="U_MEMBER")
ADMIN = build_subject(is_admin=True, platform_user_id="U_ADMIN")

HOLD = TurnHold("slack", "C_SHARED", client=False)
CLIENT_HOLD = TurnHold("slack", "C_SHARED", client=True)
HERE = Place(channel_id="C_SHARED", parent_channel_id="C_SHARED")
THREAD_HERE = Place(channel_id="C_SHARED:1700000000.000100", parent_channel_id="C_SHARED")
ELSEWHERE = Place(channel_id="C_OPS", parent_channel_id="C_OPS")
ACME = AgentRef.of("acme", "acme")


def _allowed(policy: TenantAccessPolicy, **kwargs: object) -> Decision:
    return authorize(policy, **kwargs)  # pyright: ignore[reportArgumentType]


def test_build_subject_never_makes_a_client_an_admin() -> None:
    assert build_subject(is_admin=True, platform_user_id="U", client=True).is_admin is False
    assert build_subject(is_admin=True, platform_user_id="U").is_admin is True


CLIENT_REFUSED = [
    Action.DIRECT_MESSAGE,
    Action.SAVE_ROUTINE,
    Action.ACT_FOR_CREATOR,
    Action.CREATE_AGENT,
    Action.CONFIGURE,
    Action.CHANGE_SHARED_AGENT,
    Action.FORK,
    Action.BIND_CHANNEL_DEFAULT,
    Action.MINT_CODING_TOKEN,
    Action.SET_CHANNEL_ENVIRONMENT,
    Action.SET_CHANNEL_RULE,
    Action.SET_AGENT_RULE,
    Action.SET_CHANNEL_BUDGET,
    Action.SET_CHANNEL_SKILLS,
    Action.SET_AGENT_TOOL_RULE,
    Action.SET_CHANNEL_AGENT_TOOL_RULE,
    Action.ARCHIVE_CHANNEL_COPY,
]


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("action", CLIENT_REFUSED)
@pytest.mark.parametrize("hold", [None, CLIENT_HOLD], ids=["no_hold", "held"])
def test_a_client_is_refused_what_is_never_theirs(
    policy: TenantAccessPolicy, action: Action, hold: TurnHold | None
) -> None:
    decision = _allowed(
        policy, subject=CLIENT, action=action, agent=ACME, place=HERE, origin=HERE, hold=hold
    )
    assert decision == Decision(False, "client_requester")


@pytest.mark.parametrize("family", ["spec", "attachment", "posted_token"])
def test_a_client_changes_no_shared_agent_in_any_family(family: str) -> None:
    decision = _allowed(
        OPEN,
        subject=CLIENT,
        action=Action.CHANGE_SHARED_AGENT,
        operation_family=family,
        hold=CLIENT_HOLD,
    )
    assert decision == Decision(False, "client_requester")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("action", [Action.START_TURN, Action.RUN_AGENT])
@pytest.mark.parametrize("surface", [Surface.DM, Surface.HUB, Surface.AGENT_CHAT, Surface.ROUTINE])
def test_a_client_starts_nothing_outside_a_channel(
    policy: TenantAccessPolicy, action: Action, surface: Surface
) -> None:
    decision = _allowed(
        policy, subject=CLIENT, action=action, surface=surface, agent=ACME, place=HERE
    )
    assert decision == Decision(False, "client_requester")


@pytest.mark.parametrize("policy", POLICIES)
def test_a_client_starts_and_runs_in_their_channel(policy: TenantAccessPolicy) -> None:
    agent = AgentRef.of("anyone")
    for action in (Action.START_TURN, Action.RUN_AGENT):
        decision = _allowed(
            policy,
            subject=CLIENT,
            action=action,
            agent=agent,
            place=HERE,
            hold=CLIENT_HOLD,
        )
        assert decision.allowed, action


@pytest.mark.parametrize("policy", POLICIES)
def test_a_client_publishes_only_what_they_approved(policy: TenantAccessPolicy) -> None:
    refused = _allowed(
        policy, subject=CLIENT, action=Action.PUBLISH, agent=ACME, origin=HERE, hold=CLIENT_HOLD
    )
    approved = _allowed(
        policy,
        subject=CLIENT,
        action=Action.PUBLISH,
        agent=ACME,
        origin=HERE,
        hold=CLIENT_HOLD,
        approved=True,
    )
    assert refused == Decision(False, "needs_approval")
    assert approved.allowed


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("action", [Action.READ_CHANNEL, Action.POST])
def test_a_client_with_no_origin_has_no_channel_to_be_held_to(
    policy: TenantAccessPolicy, action: Action
) -> None:
    decision = _allowed(policy, subject=CLIENT, action=action, agent=ACME, place=HERE)
    assert decision == Decision(False, "origin_required")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("action", [Action.READ_CHANNEL, Action.POST])
@pytest.mark.parametrize("hold", [HOLD, CLIENT_HOLD], ids=["member", "client"])
def test_a_held_turn_reads_and_posts_only_inside_its_channel(
    policy: TenantAccessPolicy, action: Action, hold: TurnHold
) -> None:
    agent = AgentRef.of("anyone")
    for place in (HERE, THREAD_HERE):
        assert _allowed(
            policy, subject=MEMBER, action=action, agent=agent, place=place, origin=HERE, hold=hold
        ).allowed
    outside = [
        ELSEWHERE,
        Place(),
        Place(channel_id="D_OWN", own_dm=True),
        Place(channel_id="C_SHARED_LOOKALIKE", parent_channel_id="C_SHARED_LOOKALIKE"),
    ]
    for place in outside:
        decision = _allowed(
            policy,
            subject=MEMBER,
            action=action,
            agent=agent,
            place=place,
            origin=HERE,
            hold=hold,
        )
        assert decision == Decision(False, "outside_hold"), place


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    "action", [Action.DIRECT_MESSAGE, Action.SAVE_ROUTINE, Action.CREATE_AGENT]
)
def test_a_held_turn_never_messages_saves_routines_or_creates_agents(
    policy: TenantAccessPolicy, action: Action
) -> None:
    decision = _allowed(
        policy, subject=MEMBER, action=action, agent=ACME, place=HERE, origin=HERE, hold=HOLD
    )
    assert decision == Decision(False, "outside_hold")


@pytest.mark.parametrize("policy", POLICIES)
def test_a_held_turn_publishes_only_with_approval(policy: TenantAccessPolicy) -> None:
    kwargs = {"subject": MEMBER, "action": Action.PUBLISH, "agent": ACME, "origin": HERE}
    assert _allowed(policy, **kwargs, hold=HOLD) == Decision(False, "needs_approval")
    assert _allowed(policy, **kwargs, hold=HOLD, approved=True).allowed


@pytest.mark.parametrize("policy", POLICIES)
def test_no_admin_exemption_applies_to_a_held_request(policy: TenantAccessPolicy) -> None:
    admin_dm = _allowed(
        policy,
        subject=ADMIN,
        action=Action.RUN_AGENT,
        surface=Surface.DM,
        agent=ACME,
        place=Place(),
        hold=None,
    )
    assert admin_dm.allowed, "the exemption exists without a hold"
    held = _allowed(
        policy,
        subject=ADMIN,
        action=Action.RUN_AGENT,
        surface=Surface.DM,
        agent=ACME,
        place=Place(),
        hold=HOLD,
    )
    assert held.allowed is (policy is OPEN), "held: decided like a member, not exempt"
    free = AgentRef.of("free")
    unheld_fork = _allowed(policy, subject=ADMIN, action=Action.FORK, agent=free)
    held_fork = _allowed(policy, subject=ADMIN, action=Action.FORK, agent=free, hold=HOLD)
    assert unheld_fork.allowed
    assert held_fork == Decision(False, "admin_required")
    held_config = _allowed(policy, subject=ADMIN, action=Action.SET_CHANNEL_RULE, hold=HOLD)
    assert held_config == Decision(False, "admin_required")


def test_a_held_admin_hub_read_is_not_exempt() -> None:
    session = SessionFacts(channel="C_OPS", owned=False)
    kwargs = {
        "subject": ADMIN,
        "action": Action.READ_SESSION,
        "surface": Surface.HUB,
        "agent": ACME,
        "session": session,
    }
    assert _allowed(OPEN, **kwargs).allowed
    assert _allowed(OPEN, **kwargs, hold=HOLD) == Decision(False, "outside_hold")


@pytest.mark.parametrize("action", [Action.READ_SESSION, Action.CONTINUE_SESSION])
def test_a_held_turn_reads_only_sessions_that_ran_inside_the_hold(action: Action) -> None:
    def decide(session: SessionFacts) -> Decision:
        return _allowed(OPEN, subject=MEMBER, action=action, agent=ACME, session=session, hold=HOLD)

    assert decide(SessionFacts(channel="C_SHARED")).allowed
    assert decide(SessionFacts(channel="C_SHARED", thread="C_SHARED:1700000000.000100")).allowed
    assert decide(SessionFacts(legacy_thread_id="C_SHARED")).allowed
    assert decide(SessionFacts(channel="C_OPS")) == Decision(False, "outside_hold")
    assert decide(SessionFacts()) == Decision(False, "outside_hold")
    assert decide(SessionFacts(channel="C_SHARED", private=True)) == Decision(False, "outside_hold")
    assert decide(SessionFacts(channel="C_SHARED", owned=False)) == Decision(False, "not_owner")


@pytest.mark.parametrize(
    ("policy", "agent"),
    [
        pytest.param(OPEN, AgentRef.of("daimon", "daimon"), id="default_agent"),
        pytest.param(MULTI, AgentRef.of("multi", "multi"), id="agent_bound_to_several_channels"),
    ],
)
@pytest.mark.parametrize("hold", [HOLD, CLIENT_HOLD], ids=["member", "client"])
def test_the_default_and_multi_channel_agents_still_work_in_a_shared_channel(
    policy: TenantAccessPolicy, agent: AgentRef, hold: TurnHold
) -> None:
    """The hold is a new fact, apart from the agent's home and rules."""
    subject = CLIENT if hold.client else MEMBER
    for action in (Action.START_TURN, Action.RUN_AGENT, Action.READ_CHANNEL, Action.POST):
        decision = _allowed(
            policy,
            subject=subject,
            action=action,
            agent=agent,
            place=HERE,
            origin=HERE,
            hold=hold,
        )
        assert decision.allowed, action
    assert _allowed(
        policy,
        subject=subject,
        action=Action.READ_CHANNEL,
        agent=agent,
        place=THREAD_HERE,
        origin=THREAD_HERE,
        hold=hold,
    ).allowed


@pytest.mark.parametrize(
    ("platform", "channel", "thread", "inside"),
    [
        ("slack", "C1", "C1:1700000000.000100", True),
        ("slack", "C1", "C10:1700000000.000100", False),
        ("slack", "C1", "C1", True),
        ("teams", "19:abc@thread.tacv2", "19:abc@thread.tacv2;messageid=123", True),
        ("teams", "19:abc@thread.tacv2", "19:abcd@thread.tacv2;messageid=123", False),
        ("discord", "100", "200", False),
        ("discord", "100", "100", True),
    ],
)
def test_hold_contains_each_platforms_thread_form(
    platform: str, channel: str, thread: str, inside: bool
) -> None:
    hold = TurnHold(platform, channel, client=False)
    assert hold.contains(thread) is inside


def test_hold_contains_a_discord_thread_by_its_parent_and_nothing_without_a_channel() -> None:
    hold = TurnHold("discord", "100", client=False)
    assert hold.contains("200", "100")
    assert not hold.contains(None)
    assert not hold.contains("200", "300")


@pytest.mark.parametrize("action", [Action.READ_SESSION, Action.CONTINUE_SESSION])
def test_a_client_with_no_hold_is_refused_session_access_it_owns(action: Action) -> None:
    decision = _allowed(
        OPEN,
        subject=CLIENT,
        action=action,
        agent=ACME,
        session=SessionFacts(channel="C_SHARED"),
    )
    assert decision == Decision(False, "origin_required")

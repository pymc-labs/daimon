"""A web chat runs agents that no channel rule keeps elsewhere, for everyone alike."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Decision, Place, Subject, Surface, authorize

WEB = Place(channel_id="web:t1", parent_channel_id="web")
ACME = AgentRef.of("acme", "acme")
MEMBER = Subject(is_admin=False, platform_user_id="U_MEM")
ADMIN = Subject(is_admin=True, platform_user_id="U_ADM")
VISITOR = Subject(platform_user_id="v1", client=True)


def _run(policy: TenantAccessPolicy, subject: Subject, agent: AgentRef = ACME) -> Decision:
    return authorize(
        policy,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=Surface.WEB,
        place=WEB,
        agent=agent,
    )


@pytest.mark.parametrize("subject", [MEMBER, ADMIN, VISITOR], ids=["member", "admin", "visitor"])
def test_open_agent_runs_on_the_web_for_anyone(subject: Subject) -> None:
    decision = _run(TenantAccessPolicy(), subject)
    assert decision, "an agent without a rule answers a web chat"


@pytest.mark.parametrize("subject", [MEMBER, ADMIN], ids=["member", "admin"])
@pytest.mark.parametrize(
    "policy",
    [
        TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)}),
        TenantAccessPolicy(agent_channel_pins={"acme": ()}),
        TenantAccessPolicy(
            sealed_channel_ids=("C_ACME",),
            isolated_channel_ids=("C_ACME",),
            agent_channel_pins={"acme": ("C_ACME",)},
        ),
    ],
    ids=["pinned", "nowhere", "own_agents_home"],
)
def test_channel_bound_agent_is_refused_on_the_web_even_for_an_admin(
    policy: TenantAccessPolicy, subject: Subject
) -> None:
    decision = _run(policy, subject)
    assert not decision and decision.reason == "runs_elsewhere", (
        "no admin exemption: a web chat is in none of the agent's channels"
    )


def test_admin_dm_exemption_still_holds_off_the_web() -> None:
    """The asymmetry: the same pinned agent answers an admin's DM."""
    decision = authorize(
        TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)}),
        subject=ADMIN,
        action=Action.RUN_AGENT,
        surface=Surface.DM,
        place=Place(),
        agent=ACME,
    )
    assert decision, "a DM keeps its admin exemption"


def test_other_agents_still_run_beside_a_pinned_one() -> None:
    policy = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)})
    decision = _run(policy, MEMBER, AgentRef.of("other", "other"))
    assert decision, "only the pinned agent is refused"


def _ruled(*places: str) -> TenantAccessPolicy:
    return TenantAccessPolicy(agent_rules={"acme": AgentRule(runs_in=places)})


@pytest.mark.parametrize("subject", [MEMBER, ADMIN], ids=["member", "admin"])
def test_an_agent_ruled_to_channels_and_the_web_answers_a_web_chat(subject: Subject) -> None:
    """Naming the web chat beside a channel is what lets the agent answer there."""
    assert _run(_ruled("C_ACME", "web"), subject), "the rule names the web chat"
    assert _run(_ruled("web"), subject), "a rule of the web chat alone is no channel binding"


def test_removing_a_channel_from_the_rule_keeps_the_web_chat_working() -> None:
    """Dropping one of two channels leaves the agent answering in the web chat."""
    before, after = _ruled("C_ONE", "C_TWO", "web"), _ruled("C_ONE", "web")
    assert _run(before, MEMBER) and _run(after, MEMBER), "web chat works before and after"
    assert not _run(_ruled("C_ONE"), MEMBER), "without web in the rule it stays off the web"


def test_routine_saved_from_a_web_chat_is_allowed_for_an_agent_ruled_to_the_web() -> None:
    """The web chat place is `web:<person>`; a rule names it as `web`."""

    def save(policy: TenantAccessPolicy) -> Decision:
        return authorize(
            policy,
            subject=MEMBER,
            action=Action.SAVE_ROUTINE,
            surface=Surface.WEB,
            place=Place(channel_id=WEB.channel_id),
            agent=ACME,
        )

    assert save(_ruled("web")), "a rule of the web chat covers a routine saved from it"
    assert not save(_ruled("C_ACME")), "a channel-only rule still refuses it"


def test_here_card_from_a_web_chat_says_a_web_ruled_agent_answers() -> None:
    """An agent ruled to `{"web"}` can answer in a `web:<person>` place."""
    from daimon.core.here_card import assemble_here_card

    def card_for(policy: TenantAccessPolicy) -> bool | None:
        return assemble_here_card(
            channel_id="web:t1",
            platform="web",
            agent_name="acme",
            tier="channel",
            channel=None,
            tenant=None,
            configuration_target_name=None,
            policy=policy,
            details=None,
        ).agent_can_answer_here

    assert card_for(_ruled("web")) is True, "the web rule covers the web chat place"
    assert card_for(_ruled("C_ACME")) is False, "a channel-only rule does not"

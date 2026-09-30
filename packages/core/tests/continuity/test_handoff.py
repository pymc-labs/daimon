"""Authorization for moving a task to another agent, decided without I/O."""

from __future__ import annotations

import pytest
from daimon.core.continuity.handoff import (
    HandoffAllowed,
    HandoffRefused,
    HandoffRefusedInSetupThread,
    decide_handoff,
)
from daimon.core.errors import DaimonError


def test_decide_handoff_allows_a_reachable_other_agent_when_the_thread_is_unbound() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=True,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_daimon",
    )

    assert decision == HandoffAllowed(
        destination_ma_agent_id="agt_research", destination_name="research-bot"
    ), "an ordinary thread may hand its task to a reachable agent"


def test_decide_handoff_allows_a_second_handoff_when_the_thread_already_has_one() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_stats",
        destination_name="stats-bot",
        destination_reachable=True,
        existing_binding_kind="handoff",
        origin_responder_ma_agent_id="agt_research",
    )

    assert isinstance(decision, HandoffAllowed), "a task that moved once may move again"


def test_decide_handoff_refuses_in_a_setup_thread() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=True,
        existing_binding_kind="setup",
        origin_responder_ma_agent_id="agt_daimon",
    )

    assert decision == HandoffRefused(reason="setup_thread", destination_name="research-bot"), (
        "a setup conversation must keep answering as Daimon"
    )


def test_decide_handoff_refuses_an_unreachable_destination() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=False,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_daimon",
    )

    assert decision == HandoffRefused(reason="unreachable", destination_name="research-bot"), (
        "an agent nobody can reach cannot be handed a task"
    )


def test_decide_handoff_refuses_the_agent_that_already_answers_here() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=True,
        existing_binding_kind="handoff",
        origin_responder_ma_agent_id="agt_research",
    )

    assert decision == HandoffRefused(reason="same_agent", destination_name="research-bot"), (
        "handing a task to the agent already doing it changes nothing"
    )


def test_decide_handoff_refuses_a_recreated_namesake_as_a_different_destination() -> None:
    """Identity is the id, never the name: a namesake is simply another agent."""
    decision = decide_handoff(
        destination_ma_agent_id="agt_research_v2",
        destination_name="research-bot",
        destination_reachable=True,
        existing_binding_kind="handoff",
        origin_responder_ma_agent_id="agt_research_v1",
    )

    assert isinstance(decision, HandoffAllowed), (
        "the recreated agent is a distinct identity, so the move is a real move"
    )
    assert decision.destination_ma_agent_id == "agt_research_v2", (
        "the concrete id chosen by the caller is what the handoff binds"
    )


def test_decide_handoff_reports_the_setup_thread_before_any_other_refusal() -> None:
    """Every refusal at once: the location is the one that cannot be worked around."""
    decision = decide_handoff(
        destination_ma_agent_id="agt_daimon",
        destination_name="daimon",
        destination_reachable=False,
        existing_binding_kind="setup",
        origin_responder_ma_agent_id="agt_daimon",
    )

    assert isinstance(decision, HandoffRefused) and decision.reason == "setup_thread", (
        "a setup conversation refuses first, whatever else is wrong"
    )


def test_decide_handoff_reports_unreachable_before_same_agent() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=False,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_research",
    )

    assert isinstance(decision, HandoffRefused) and decision.reason == "unreachable", (
        "reachability is the fact the caller can act on"
    )


def test_handoff_refused_in_setup_thread_is_a_daimon_error() -> None:
    """Adapters catch `DaimonError` at their edge; this refusal must land there."""
    with pytest.raises(DaimonError):
        raise HandoffRefusedInSetupThread("setup conversation")


def test_decide_handoff_refuses_an_agent_pinned_to_other_channels() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_rx",
        destination_name="daimon-rx",
        destination_reachable=True,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_daimon",
        destination_pinned_elsewhere=True,
    )

    assert decision == HandoffRefused(reason="pinned_elsewhere", destination_name="daimon-rx"), (
        "a pinned agent is reachable in its own channels, which must not carry it here"
    )


def test_decide_handoff_setup_thread_refusal_wins_over_the_pin() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_rx",
        destination_name="daimon-rx",
        destination_reachable=True,
        existing_binding_kind="setup",
        origin_responder_ma_agent_id="agt_daimon",
        destination_pinned_elsewhere=True,
    )

    assert isinstance(decision, HandoffRefused)
    assert decision.reason == "setup_thread"


@pytest.mark.parametrize(
    ("answers_channel", "is_admin", "allowed"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["channel-agent", "admin", "member-to-other-agent"],
)
def test_decide_handoff_to_an_agent_the_channel_does_not_answer_with_is_admin_only(
    answers_channel: bool, is_admin: bool, allowed: bool
) -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_acme",
        destination_name="acme-project",
        destination_reachable=True,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_daimon",
        destination_answers_channel=answers_channel,
        caller_is_admin=is_admin,
    )

    if allowed:
        assert isinstance(decision, HandoffAllowed)
    else:
        assert decision == HandoffRefused(
            reason="admin_required", destination_name="acme-project"
        ), "a member must not bring another project's agent into this channel"

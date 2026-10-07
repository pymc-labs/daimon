"""Authorization for moving a task to another agent, decided without I/O."""

from __future__ import annotations

import pytest
from daimon.core.authz import ALLOW, Decision
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
        access=ALLOW,
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
        access=ALLOW,
    )

    assert isinstance(decision, HandoffAllowed), "a task that moved once may move again"


def test_decide_handoff_refuses_in_a_setup_thread() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_research",
        destination_name="research-bot",
        destination_reachable=True,
        existing_binding_kind="setup",
        origin_responder_ma_agent_id="agt_daimon",
        access=ALLOW,
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
        access=ALLOW,
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
        access=ALLOW,
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
        access=ALLOW,
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
        access=ALLOW,
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
        access=ALLOW,
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
        access=Decision(False, "runs_elsewhere"),
    )

    assert decision == HandoffRefused(
        reason="runs_elsewhere",
        destination_name="daimon-rx",
        authz_reason="runs_elsewhere",
    ), "an agent with a rule is reachable in its own channels, which must not carry it here"


def test_decide_handoff_setup_thread_refusal_wins_over_the_agent_rule() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_rx",
        destination_name="daimon-rx",
        destination_reachable=True,
        existing_binding_kind="setup",
        origin_responder_ma_agent_id="agt_daimon",
        access=Decision(False, "runs_elsewhere"),
    )

    assert isinstance(decision, HandoffRefused)
    assert decision.reason == "setup_thread"


@pytest.mark.parametrize(
    ("denied", "reason"),
    [
        ("writers_none", "writers_none"),
        ("invoker_not_allowed", "invoker_not_allowed"),
        ("own_agents_only", "own_agents_only"),
        ("runs_elsewhere", "runs_elsewhere"),
        ("admin_required", "admin_required"),
        ("not_a_reader", "not_a_reader"),
    ],
)
def test_decide_handoff_refuses_what_authorize_denied(denied: str, reason: str) -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_acme",
        destination_name="acme-project",
        destination_reachable=True,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_daimon",
        access=Decision(False, denied),  # pyright: ignore[reportArgumentType]
    )

    assert decision == HandoffRefused(
        reason=reason,  # pyright: ignore[reportArgumentType]
        destination_name="acme-project",
        authz_reason=denied,
    ), "the refusal keeps the policy's reason for the audit trail"


def test_decide_handoff_says_same_agent_before_who_may_bring_it_in() -> None:
    """The agent already answering here is not "brought in" by anyone."""
    decision = decide_handoff(
        destination_ma_agent_id="agt_acme",
        destination_name="acme-project",
        destination_reachable=True,
        existing_binding_kind="handoff",
        origin_responder_ma_agent_id="agt_acme",
        access=Decision(False, "admin_required"),
    )

    assert isinstance(decision, HandoffRefused) and decision.reason == "same_agent"


def test_decide_handoff_says_runs_elsewhere_before_unreachable_or_same_agent() -> None:
    decision = decide_handoff(
        destination_ma_agent_id="agt_acme",
        destination_name="acme-project",
        destination_reachable=False,
        existing_binding_kind=None,
        origin_responder_ma_agent_id="agt_acme",
        access=Decision(False, "runs_elsewhere"),
    )

    assert isinstance(decision, HandoffRefused) and decision.reason == "runs_elsewhere"

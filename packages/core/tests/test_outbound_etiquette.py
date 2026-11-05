"""Outbound etiquette: mention rendering, attribution and the reach budget."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.confirmation import confirmed_tools_for_turn, prompt_for_tool_call
from daimon.core.outbound.mentions import (
    attribution_line,
    caption_length,
    mentioned_user_ids,
    neutralise_broadcasts,
    render_post,
)
from daimon.core.outbound.reach import (
    CLIENT_WINDOW,
    ReachPlan,
    ReachTally,
    card_title,
    check_reach,
    client_tally_key,
)
from daimon.core.posted_controls.confirmation import build_confirmation_card
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy, decide_tool_call
from daimon.core.turn.deps import TurnDeps

LIMITS = ReachPlan(threads=2, places=2, people=5)


@pytest.mark.parametrize(
    ("platform", "text", "expected"),
    [
        ("discord", "hi @everyone and @here", "hi @​everyone and @​here"),
        ("slack", "hi <!channel> <!here|here> <!subteam^S1|@devs>", "hi @channel @here @devs"),
        ("teams", "hi <at>Everyone</at>", "hi @Everyone"),
    ],
)
def test_neutralise_broadcasts_when_group_ping_then_plain_text(platform, text, expected):
    assert neutralise_broadcasts(text, platform) == expected, f"{platform} group pings stay text"


def test_mentioned_user_ids_when_teams_then_none_because_posts_carry_no_entities():
    assert mentioned_user_ids("<at>Ann</at>", "teams") == (), "Teams tool posts ping nobody"
    assert mentioned_user_ids("<@123> <@!456>", "discord") == ("123", "456"), "both forms"
    assert mentioned_user_ids("<@U1|ann>", "slack") == ("U1",), "Slack ids"


@pytest.mark.parametrize(
    ("platform", "kwargs", "expected"),
    [
        ("discord", {"requester_id": "42"}, "-# requested by <@42>"),
        ("slack", {"requester_name": "Ann"}, "requested by Ann"),
        ("teams", {"requester_name": "Ann"}, "requested by Ann"),
        ("slack", {"routine_name": "daily"}, 'from the routine "daily"'),
    ],
)
def test_attribution_line_when_requester_known_then_platform_form(platform, kwargs, expected):
    line = attribution_line(
        platform,
        requester_id=kwargs.get("requester_id"),
        requester_name=kwargs.get("requester_name"),
        routine_name=kwargs.get("routine_name"),
    )
    assert line == expected, f"{platform} attribution"


def test_render_post_when_attributed_twice_then_line_appears_once():
    once = render_post("done", "discord", attribution="-# requested by <@42>")
    twice = render_post(once.text, "discord", attribution="-# requested by <@42>")
    assert twice.text == "done\n-# requested by <@42>", "the line is never duplicated"
    assert twice.user_ids == (), "the requester's attribution is not a tag that counts"


def test_render_post_when_person_opted_out_then_plain_name_and_not_counted():
    post = render_post("<@1> and <@2>", "discord", do_not_tag=frozenset({"2"}), names={"2": "Bo"})
    assert post.text == "<@1> and @Bo", "an opted-out person becomes a plain name"
    assert post.user_ids == ("1",), "opted-out people never count"


def test_caption_length_when_only_links_and_tags_then_zero():
    assert caption_length("<@1> https://x.example/v.mp4", "discord") == 0, "nothing said"


def test_check_reach_when_within_limits_then_ok():
    verdict = check_reach(
        plan=ReachPlan(threads=2, places=1, people=3),
        so_far=ReachTally(),
        call=ReachTally(threads=2, people=frozenset({"1", "2"})),
        limits=LIMITS,
        approved=None,
    )
    assert verdict == "ok", "two threads and two people fit"


def test_check_reach_when_plan_over_limit_then_asks_before_first_post():
    verdict = check_reach(
        plan=ReachPlan(threads=10, places=0, people=3),
        so_far=ReachTally(),
        call=ReachTally(threads=1),
        limits=LIMITS,
        approved=None,
    )
    assert verdict == "needs_approval", "the plan is counted before anything posts"


@pytest.mark.parametrize(("so_far", "expected"), [(1, "ok"), (2, "needs_approval")])
def test_check_reach_when_actual_passes_plan_then_actual_counts(so_far, expected):
    verdict = check_reach(
        plan=ReachPlan(threads=0, places=0, people=0),
        so_far=ReachTally(threads=so_far),
        call=ReachTally(threads=1),
        limits=LIMITS,
        approved=None,
    )
    assert verdict == expected, "a plan below actual is never refused on its own"


def test_check_reach_when_approved_then_ok_until_past_approval():
    approved = ReachPlan(threads=10, places=0, people=3)
    ok = check_reach(
        plan=approved,
        so_far=ReachTally(threads=9),
        call=ReachTally(threads=1),
        limits=LIMITS,
        approved=approved,
    )
    again = check_reach(
        plan=ReachPlan(threads=11, places=0, people=3),
        so_far=ReachTally(threads=10),
        call=ReachTally(threads=1),
        limits=LIMITS,
        approved=approved,
    )
    assert (ok, again) == ("ok", "needs_approval"), "past the approved plan it asks again"


@pytest.mark.parametrize(
    ("plan", "where", "expected"),
    [
        (
            ReachPlan(threads=10, people=3),
            "#launch",
            "Open 10 threads in #launch and tag 3 people?",
        ),
        (ReachPlan(threads=8), "", "Open 8 threads?"),
        (ReachPlan(threads=1, people=1), "", "Open 1 thread and tag 1 person?"),
        (ReachPlan(places=4, people=6), "", "Post in 4 places and tag 6 people?"),
        (ReachPlan(places=2), "#a, #b", "Post in #a, #b?"),
        (
            ReachPlan(threads=5, places=3, people=2),
            "#a, #b and a DM",
            "Open 5 threads in #a, #b and a DM and tag 2 people?",
        ),
        (ReachPlan(people=3), "", "Tag 3 people?"),
    ],
    ids=[
        "threads_where_people",
        "own_threads",
        "singulars",
        "posts_places",
        "posts_where",
        "several_places",
        "people_only",
    ],
)
def test_card_title_when_plan_given_then_one_short_question(plan, where, expected):
    title = card_title(plan, where)
    assert title == expected, "one phrase from the plan, capitalised, ending in a question mark"
    assert "Daimon" not in title and "(" not in title and "across" not in title, "plain words only"


def _reach_call() -> ToolCall:
    return ToolCall(
        tool_use_id="reach",
        server_name=DAIMON_SERVER_NAME,
        tool_name="approve_reach",
        input={"threads": 10, "places": 0, "people": 3, "where": "#x"},
    )


def test_approve_reach_when_attended_then_card_else_denied():
    policy = ToolSafetyPolicy(enabled=False)
    selected = confirmed_tools_for_turn(
        cast(
            TurnDeps,
            SimpleNamespace(
                backend_selection_enabled=False, microsoft_send=False, tool_safety=policy
            ),
        )
    )
    outcomes = [
        decide_tool_call(
            policy,
            _reach_call(),
            attended=attended,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            confirmed_tools=selected,
        ).outcome
        for attended in (True, False)
    ]
    assert outcomes == ["ask", "deny"], "a card when someone is there, else fail closed"


def test_approve_reach_prompt_when_built_then_neutral_card_text():
    prompt = prompt_for_tool_call(
        _reach_call(), requester_platform_user_id="owner", now=datetime.now(UTC)
    )
    assert prompt.title == "Open 10 threads in #x and tag 3 people?", "the question is the title"
    assert prompt.consequence is None, "no second line under the question"
    assert prompt.action is None, "the approved card reads the question without its mark"
    assert prompt.denied_action == "Nothing posted", "denied says nothing went out"


@pytest.mark.parametrize(
    "raw",
    [
        {"threads": 10, "places": 2, "people": 3},
        {"threads": "10", "places": "2", "people": "3"},
        {"threads": 10.0, "places": 2.0, "people": 3.0},
    ],
    ids=["ints", "numeric_strings", "whole_floats"],
)
def test_approve_reach_prompt_when_args_coerce_then_card_shows_the_stored_plan(raw):
    call = _reach_call().model_copy(update={"input": {**raw, "where": "#x"}})
    prompt = prompt_for_tool_call(call, requester_platform_user_id="owner", now=datetime.now(UTC))
    stored = ReachPlan.from_args(raw)
    assert stored == ReachPlan(threads=10, places=2, people=3), "the tool stores the counts"
    assert prompt.title == card_title(stored, "#x"), "the card shows exactly what is stored"


@pytest.mark.parametrize(
    "raw",
    [{"threads": "lots", "places": 0, "people": 3}, {"threads": -1}, {"places": 1, "people": 1}],
    ids=["word", "negative", "missing"],
)
def test_approve_reach_prompt_when_counts_invalid_then_no_card(raw):
    call = _reach_call().model_copy(update={"input": {**raw, "where": "#x"}})
    with pytest.raises(ValueError, match="validation error"):
        prompt_for_tool_call(call, requester_platform_user_id="owner", now=datetime.now(UTC))


def test_client_tally_key_when_window_turns_then_budget_renews() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    same = client_tally_key("jti", start + CLIENT_WINDOW - timedelta(seconds=1))
    later = client_tally_key("jti", start + CLIENT_WINDOW)

    assert client_tally_key("jti", start) == same, "one window shares a tally"
    assert later != same, "the next window starts a fresh tally"


def test_approve_reach_card_when_settled_then_states_read_the_question():
    prompt = prompt_for_tool_call(
        _reach_call(), requester_platform_user_id="owner", now=datetime.now(UTC)
    )
    pending = build_confirmation_card(prompt, state="pending", token="token-12345")
    approved = build_confirmation_card(prompt, state="approved")
    denied = build_confirmation_card(prompt, state="denied")
    assert (pending.headline, pending.consequence, pending.body) == (
        "Open 10 threads in #x and tag 3 people?",
        None,
        None,
    ), "pending is one question above the buttons"
    assert approved.body == "Open 10 threads in #x and tag 3 people", "approved repeats it plainly"
    assert denied.body == "Nothing posted", "denied says nothing went out"

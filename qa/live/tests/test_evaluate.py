from __future__ import annotations

import pytest

from qa.live.evaluate import evaluate
from qa.live.schema import Assertion
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Turn, utcnow


@pytest.fixture
def turn(backend: FakeBackend) -> Turn:
    turn = Turn(1, "1", "parent", utcnow())
    backend.collect(turn, 10)
    return turn


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "reply_within_s", "max": 3},
        {"kind": "done_within_s", "max": 3},
        {"kind": "no_silent_drop", "max": 3},
        {"kind": "in_thread"},
        {"kind": "no_channel_post"},
        {"kind": "text_present", "pattern": "APPLE"},
        {"kind": "text_absent", "pattern": "ORANGE"},
        {"kind": "card_finalized"},
        {"kind": "reaction_present", "emoji": "👍"},
        {"kind": "attachments", "min": 1, "max": 1, "unique": True},
        {"kind": "log_absent", "event": "session.replaced"},
        {"kind": "judge", "rubric": "Contains APPLE"},
    ],
)
def test_each_assertion(
    assertion: dict[str, object], turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    check = evaluate(Assertion.model_validate(assertion | {"turn": 1}), [turn], backend, judge)
    assert check.status == "PASS"
    assert check.evidence


def test_db_and_log_present(turn: Turn, backend: FakeBackend, judge: FakeJudge) -> None:
    check = evaluate(
        Assertion(kind="db_check", sql="SELECT 1 n", expect=[{"n": 1}]), [], backend, judge
    )
    assert check.status == "PASS"
    backend.log_rows = [{"jsonPayload": {"event": "a"}}]
    assert (
        evaluate(Assertion(kind="log_present", turn=1, event="a"), [turn], backend, judge).status
        == "PASS"
    )


def test_unavailable_logs_do_not_prove_absence(
    turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    backend.log_error = True
    assert (
        evaluate(Assertion(kind="log_absent", turn=1, event="a"), [turn], backend, judge).status
        == "PENDING"
    )


def test_judge_execution_error_is_pending_and_redacts_exception(
    turn: Turn, backend: FakeBackend
) -> None:
    class BrokenJudge(FakeJudge):
        def evaluate(self, rubric: str, answer: str) -> tuple[bool, str]:
            raise ValueError("secret provider response")

    check = evaluate(
        Assertion(kind="judge", turn=1, rubric="contains APPLE"),
        [turn],
        backend,
        BrokenJudge(),
    )
    assert check.status == "PENDING"
    assert check.reason == "judge execution unavailable: ValueError"
    assert check.evidence


def test_footer_and_field_regex(turn: Turn, backend: FakeBackend, judge: FakeJudge) -> None:
    turn.messages[0]["embeds"] = [
        {
            "title": "TITLE",
            "description": "DESC",
            "fields": [{"name": "NAME", "value": "VALUE"}],
            "footer": {"text": "FOOTER"},
        }
    ]
    for pattern in ["TITLE", "DESC", "NAME", "VALUE", "FOOTER"]:
        assert (
            evaluate(
                Assertion(kind="text_present", turn=1, pattern=pattern), [turn], backend, judge
            ).status
            == "PASS"
        )


def test_previous_attachment_is_not_unique(
    turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    previous = Turn(1, "p", "parent", utcnow(), messages=turn.messages)
    turn.number = 2
    assert (
        evaluate(
            Assertion(kind="attachments", turn=2, unique=True), [previous, turn], backend, judge
        ).status
        == "FAIL"
    )


def test_no_output_does_not_pass_text_absence(
    turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    turn.messages = []
    assert (
        evaluate(
            Assertion(kind="text_absent", turn=1, pattern="bad"), [turn], backend, judge
        ).status
        == "PENDING"
    )
    assert (
        evaluate(Assertion(kind="done_within_s", turn=2, maximum=3), [turn], backend, judge).status
        == "PENDING"
    )


def test_working_card_and_parent_post_fail(
    turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    turn.messages[0]["working"] = True
    turn.parent_messages = [{"id": "9", "content": "unexpected", "type": 0}]
    for kind in ["card_finalized", "no_channel_post"]:
        assert (
            evaluate(
                Assertion.model_validate({"kind": kind, "turn": 1}), [turn], backend, judge
            ).status
            == "FAIL"
        )


def test_reaction_assertion_uses_progress_history(
    turn: Turn, backend: FakeBackend, judge: FakeJudge
) -> None:
    turn.trigger_reactions = []
    assertion = Assertion(kind="reaction_present", turn=1, emoji="👀")
    assert evaluate(assertion, [turn], backend, judge).status == "FAIL"
    turn.trigger_reaction_history = [
        {"elapsed_s": 0.5, "reactions": [{"emoji": {"name": "👀"}}]},
        {"elapsed_s": 2.0, "reactions": []},
    ]
    assert evaluate(assertion, [turn], backend, judge).status == "PASS"


@pytest.mark.parametrize(
    "observed_s,first_poll,until,settled,classification,expected",
    [
        (2.0, 1.0, 15.0, False, "working", "PASS"),
        (20.0, 20.0, 30.0, True, "working", "PENDING"),
        (20.0, 1.0, 30.0, True, "working", "FAIL"),
        (2.0, 1.0, 15.0, True, "answered", "FAIL"),
        (None, None, None, False, "working", "PENDING"),
        (None, 1.0, 8.0, False, "working", "PENDING"),
    ],
)
def test_progress_text_requires_a_real_early_nonterminal_snapshot(
    observed_s: float | None,
    first_poll: float | None,
    until: float | None,
    settled: bool,
    classification: str,
    expected: str,
    turn: Turn,
    backend: FakeBackend,
    judge: FakeJudge,
) -> None:
    turn.progress_first_poll_s = first_poll
    turn.progress_observed_until_s = until
    turn.settled = settled
    turn.progress_text_history = (
        []
        if observed_s is None
        else [
            {
                "elapsed_s": observed_s,
                "classification": classification,
                "message": {"content": "Queued for a free slot"},
            }
        ]
    )
    check = evaluate(
        Assertion(kind="progress_text_seen", turn=1, pattern="(?i)queued", within_s=15),
        [turn],
        backend,
        judge,
    )
    assert check.status == expected


@pytest.mark.parametrize(
    "classification,expected",
    [("working", "PASS"), ("component_state_unknown", "PENDING"), ("other_embed", "PENDING")],
)
def test_progress_nested_component_copy_requires_known_running_state(
    classification: str,
    expected: str,
    turn: Turn,
    backend: FakeBackend,
    judge: FakeJudge,
) -> None:
    turn.progress_first_poll_s = 1.0
    turn.progress_observed_until_s = 20.0
    turn.progress_text_history = [
        {
            "elapsed_s": 2.0,
            "classification": classification,
            "message": {
                "components": [
                    {
                        "type": 17,
                        "components": [
                            {
                                "type": 9,
                                "components": [{"type": 10, "content": "Queued for a free slot"}],
                            }
                        ],
                    }
                ]
            },
        }
    ]
    check = evaluate(
        Assertion(kind="progress_text_seen", turn=1, pattern="Queued", within_s=15),
        [turn],
        backend,
        judge,
    )
    assert check.status == expected


def test_component_text_display_ignores_button_labels() -> None:
    from qa.live.types import component_text

    assert component_text(
        {
            "components": [
                {
                    "type": 17,
                    "components": [
                        {"type": 10, "content": "Visible copy"},
                        {"type": 1, "components": [{"type": 2, "label": "Queued button"}]},
                    ],
                }
            ]
        }
    ) == ["Visible copy"]

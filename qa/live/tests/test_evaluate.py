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

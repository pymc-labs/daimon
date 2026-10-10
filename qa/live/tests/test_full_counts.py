from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from qa.live.evaluate import evaluate
from qa.live.schema import Assertion, Scenario, load_catalog
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Check, Message, Turn, utcnow


def check(kind: str, messages: list[Message], **params: JsonValue) -> Check:
    turn = Turn(1, "0", "parent", utcnow(), ended_at=utcnow(), messages=messages, settled=True)
    return evaluate(
        Assertion.model_validate({"kind": kind, "turn": 1, **params}),
        [turn],
        FakeBackend(),
        FakeJudge(),
    )


def test_message_count_deduplicates_and_excludes_thread_starter() -> None:
    messages: list[Message] = [
        {"id": "1", "type": 21},
        {"id": "2", "content": "Hi"},
        {"id": "2", "content": "Hi"},
        {"id": "3", "content": "Again"},
    ]
    assert check("message_count", messages, min=2, max=2).status == "PASS"
    assert check("message_count", messages, max=1).status == "FAIL"
    assert check("message_count", [], min=1).status == "FAIL"
    assert check("message_count", [{"content": "Hi"}], max=1).status == "PENDING"


def test_unfinished_observation_cannot_prove_zero_messages() -> None:
    turn = Turn(1, "0", "parent", utcnow())
    assertion = Assertion(kind="message_count", turn=1, maximum=0)
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"


def test_fences_are_balanced_in_each_independently_rendered_component() -> None:
    assert check("fences_balanced", [{"id": "1", "content": "```py\nx = 1\n```"}]).status == "PASS"
    assert check("fences_balanced", [{"id": "1", "content": "```py\nx = 1"}]).status == "FAIL"
    assert (
        check(
            "fences_balanced",
            [{"id": "1", "content": "```py\nx = 1", "embeds": [{"description": "```"}]}],
        ).status
        == "FAIL"
    )
    assert check("fences_balanced", []).status == "FAIL"


def footer(text: str = "1s · $0.012 used · $9.988 left") -> list[JsonValue]:
    return [{"footer": {"text": text}}]


def test_cost_footer_must_be_on_last_answer_not_a_file_only_post() -> None:
    messages: list[Message] = [
        {"id": "3", "content": "Done", "embeds": footer()},
        {"id": "2", "content": "Earlier"},
    ]
    assert check("footer_on_last_message", messages).status == "PASS"
    messages[1]["embeds"] = footer()
    assert check("footer_on_last_message", messages).status == "FAIL"
    messages[1].pop("embeds")
    messages.append({"id": "4", "attachments": [{"filename": "report.pdf"}]})
    assert check("footer_on_last_message", messages).status == "FAIL"
    messages[0].pop("embeds")
    messages[-1]["embeds"] = footer()
    assert check("footer_on_last_message", messages).status == "FAIL"
    messages[-1]["content"] = "Attached report"
    assert check("footer_on_last_message", messages).status == "PASS"
    assert check("footer_on_last_message", [{"id": "1", "content": "$0.012 used"}]).status == "FAIL"
    assert (
        check(
            "footer_on_last_message",
            [{"id": "1", "content": "Done", "embeds": footer("<$0.001 used")}],
        ).status
        == "PASS"
    )


def test_filtered_attachment_counts_do_not_count_unmatched_files() -> None:
    messages: list[Message] = [
        {
            "id": "1",
            "attachments": [
                {"filename": "report.PDF", "size": 10},
                {"filename": "data.csv", "size": 20},
            ],
        }
    ]
    assert check("attachments", messages, min=1, max=1, name_pattern=r"(?i)\.pdf$").status == "PASS"
    assert check("attachments", messages, max=0, name_pattern=r"\.typ$").status == "PASS"
    assert check("attachments", messages, max=0, name_pattern=r"\.csv$").status == "FAIL"
    assert check("attachments", [], max=0, name_pattern=r"\.pdf$").status == "PASS"
    assert check("attachments", [], min=1, name_pattern=r"\.pdf$").status == "FAIL"


def test_uniqueness_is_only_among_filtered_files() -> None:
    prior = Turn(
        1,
        "1",
        "parent",
        utcnow(),
        messages=[{"id": "2", "attachments": [{"filename": "same.csv", "size": 1}]}],
    )
    turn = Turn(
        2,
        "3",
        "parent",
        utcnow(),
        ended_at=utcnow(),
        settled=True,
        messages=[
            {
                "id": "4",
                "attachments": [
                    {"filename": "same.csv", "size": 1},
                    {"filename": "report.pdf", "size": 2},
                ],
            }
        ],
    )
    assertion = Assertion(
        kind="attachments", turn=2, minimum=1, maximum=1, unique=True, name_pattern=r"\.pdf$"
    )
    assert evaluate(assertion, [prior, turn], FakeBackend(), FakeJudge()).status == "PASS"
    prior.messages[0]["attachments"] = [{"filename": "report.pdf", "size": 2}]
    assert evaluate(assertion, [prior, turn], FakeBackend(), FakeJudge()).status == "FAIL"


def test_thread_name_checks_every_requested_predicate() -> None:
    assert (
        check("thread_name", [], pattern="Inventory", pattern_absent="Greeting", max_len=50).status
        == "PASS"
    )
    assert check("thread_name", [], pattern_absent="Inventory").status == "FAIL"
    assert check("thread_name", [], pattern="Greeting").status == "FAIL"
    assert check("thread_name", [], max_len=5).status == "FAIL"


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "message_count", "max": 1.5},
        {"kind": "thread_name"},
        {"kind": "thread_name", "pattern_absent": "["},
        {"kind": "thread_name", "max_len": 0},
        {"kind": "text_present", "pattern": "a", "max_len": 5},
    ],
)
def test_invalid_count_or_name_contract_fails(data: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        Assertion.model_validate({"turn": 1, **data})


def test_catalog_loads_new_kinds_and_filtered_absence(tmp_path: Path) -> None:
    (tmp_path / "full.yaml").write_text("""
id: QA-FULL-PDF
set: A
surface: discord
tier: full
priority: P1
title: PDF output
est_turns: 1
friction: []
sources: []
steps:
  - do: mention
    text: Attach a PDF
assert:
  - kind: message_count
    turn: 1
    max: 2
  - kind: fences_balanced
    turn: 1
  - kind: footer_on_last_message
    turn: 1
  - kind: thread_name
    turn: 1
    pattern_absent: Greeting
  - kind: attachments
    turn: 1
    max: 0
    name_pattern: '\\.typ$'
""")
    assert isinstance(load_catalog(tmp_path)[0], Scenario)

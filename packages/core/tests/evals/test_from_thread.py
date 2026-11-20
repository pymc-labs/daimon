"""Cases from threads: one per answered turn, no later data in a world, drafts and near-duplicates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from daimon.core.evals.dedupe import group_id, groups, thin
from daimon.core.evals.draft import Completion, draft_case, estimate_draft
from daimon.core.evals.from_thread import (
    ThreadCall,
    ThreadMessage,
    ThreadPerson,
    ThreadRecord,
    build_cases,
)
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.validate import case_problems

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ASKER, BOT = "u1", "bot"


def _record(texts: list[str], events: bool = True) -> ThreadRecord:
    messages: list[ThreadMessage] = []
    for n, text in enumerate(texts, start=1):
        at = T0 + timedelta(minutes=n)
        call = ThreadCall(tool=f"tool_{n}", args={"n": n}, result=f"RESULT-{n}")
        messages.append(ThreadMessage(id=f"q{n}", author=ASKER, at=at, text=text, calls=[call]))
        messages.append(ThreadMessage(id=f"a{n}", author=BOT, at=at, text=f"Answer {n}"))
    return ThreadRecord(
        platform="slack", ref="t", events_recorded=events,
        people=[ThreadPerson(id=ASKER, name="Pat Doe"), ThreadPerson(id=BOT, name="Bot", is_bot=True)],
        messages=messages,
    )  # fmt: skip


def _cases(record: ThreadRecord) -> list[dict[str, object]]:
    return [
        b.case for b in build_cases(record, Scrubber(record.entities(), b"k" * 20, ner_model=None))
    ]


def test_thread_when_three_turns_then_three_cases_and_no_world_from_later_turns() -> None:
    cases = _cases(_record(["first", "second", "third"]))
    assert len(cases) == 3, "one case per answered turn"
    for number, case in enumerate(cases, start=1):
        world = json.dumps(case["world"])
        assert f"RESULT-{number}" in world, "the turn's own call is in its world"
        assert not any(f"RESULT-{later}" in world for later in range(number + 1, 4)), (
            "no later data"
        )
        assert len(case["history"]) == 2 * (number - 1), "history is what came before"  # type: ignore[arg-type]
        assert case_problems(case) == [], "valid"
    assert len({c["id"] for c in cases}) == 3, "each case has its own id"
    assert cases[0]["fidelity"] == {"level": "full"}, "calls recorded in full"


def test_thread_when_no_events_then_text_fidelity() -> None:
    (case,) = _cases(_record(["only"], events=False))
    assert case["fidelity"]["level"] == "text", "no tool calls were recorded"  # type: ignore[index]


def test_dedupe_when_requests_repeat_then_grouped_and_thinned() -> None:
    cases = _cases(
        _record(
            [
                "please list the open pull requests",
                "Please list the open pull requests!",
                "book a room",
            ]
        )
    )
    found = groups(cases)
    assert len(found) == 1 and len(found[0]) == 2, "the repeat is a group of two"
    assert thin(found, 1) == [found[0][1]], "the newer one is dropped"
    assert group_id(found[0]).startswith("dup-"), "groups have a stable name"


async def test_draft_when_model_answers_then_expectations_are_set_and_flagged() -> None:
    (case,) = _cases(_record(["list the pull requests"]))
    seen: list[str] = []

    async def complete(system: str, prompt: str, max_tokens: int) -> Completion:
        seen.append(prompt)
        text = '{"outputs": [{"kind": "reply", "text": {"op": "contains", "value": "Answer"}}], '
        text += '"rubric": [{"id": "r1", "criterion": "Lists the pull requests"}]}'
        return Completion(text=text, input_tokens=10, output_tokens=5, model="m")

    drafted, _ = await draft_case(complete, case)
    expect = drafted["expect"]
    assert expect["rubric"][0]["id"] == "r1" and expect["reference"]["text"] == "Answer 1"  # type: ignore[index]
    assert {"reason": "expectations_unreviewed"} in drafted["review_flags"], "flagged"  # type: ignore[operator]
    assert drafted["provenance"]["expectations_by"][0]["action"] == "drafted"  # type: ignore[index]
    assert case_problems(drafted) == [], "still a valid case"
    estimate = estimate_draft([case], "claude-haiku-5-5")
    assert estimate.cases == 1 and estimate.usd > 0, "an estimate comes before any call"

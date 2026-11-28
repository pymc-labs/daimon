"""A workflow's runs as a board and a calendar: the stored card, the stage, the view queries."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.core.workflows import board, service
from daimon.core.workflows.access import Who
from daimon.core.workflows.engine import RunFacts, StepView
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import Spec, parse_spec
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

NODES: dict[str, Any] = {
    "b": {
        "kind": "branch",
        "cases": [
            {"when": "${{ inputs.mode == 'done' }}", "to": "f"},
            {"when": "${{ inputs.mode == 'bad' }}", "to": "x"},
        ],
        "else": "draft",
    },
    "draft": {"kind": "transform", "stage": "Draft", "set": {"a": "1"}, "next": "review"},
    "review": {"kind": "approval", "stage": "Review", "next": "f"},
    "x": {"kind": "transform", "set": {"a": "${{ 1 / 0 }}"}, "next": "f"},
    "f": {"kind": "finish"},
}
CARD: dict[str, Any] = {
    "fields": {
        "title": "${{ inputs.title }}",
        "broken": "${{ 1 / 0 }}",
        "due": "${{ inputs.due }}",
        "step": "${{ has(steps.draft) ? steps.draft.output.a : 'none' }}",
    },
    "date": "due",
}


def _spec(nodes: dict[str, Any] | None = None) -> Spec:
    return parse_spec(
        {
            "schema": "daimon.workflow/v1",
            "name": "T",
            "start": "b",
            "nodes": nodes or NODES,
            "card": CARD,
        }
    )


def test_a_card_field_that_errors_is_null_and_the_others_render() -> None:
    facts = RunFacts({"title": "Q4", "due": "2026-11-05"}, {}, {}, {}, NOW)
    card = board.render_card(
        _spec(), [StepView("draft", 1, "succeeded", output={"a": "1"})], facts, now=NOW
    )
    assert card == {"title": "Q4", "broken": None, "due": "2026-11-05", "step": "1"}, card
    plain = parse_spec({**_spec().model_dump(by_alias=True), "card": None})
    assert board.render_card(plain, [], facts, now=NOW) is None, "no card, no render"


@pytest.mark.parametrize(
    ("steps", "status", "stage"),
    [
        ([("draft", "succeeded"), ("review", "waiting")], "waiting", "Review"),
        ([("draft", "running")], "running", "Draft"),
        ([("draft", "succeeded"), ("x", "running")], "running", "Draft"),
        ([], "queued", "Draft"),
        ([("review", "succeeded")], "succeeded", "Done"),
        ([("draft", "failed")], "failed", "Failed"),
        ([("draft", "succeeded")], "cancelled", "Failed"),
    ],
)
def test_the_stage_is_read_from_the_current_step(
    steps: list[tuple[str, str]], status: str, stage: str
) -> None:
    assert board.stage_of(_spec(), steps, status) == stage, (steps, status)


def test_without_stages_running_runs_share_one_column() -> None:
    plain = _spec({"f": {"kind": "finish"}})
    assert board.stage_of(plain, [("f", "running")], "running") == board.ACTIVE


async def _runs(world: World) -> dict[str, Any]:
    review = await world.run(inputs={"mode": "go", "title": "Q4", "due": "2026-11-05"})
    await world.park(review.id, "review")
    done = await world.run(inputs={"mode": "done", "title": "Q3", "due": "2026-12-01"})
    bad = await world.run(inputs={"mode": "bad", "title": "Q2", "due": "2026-11-20"})
    return {"review": review, "done": done, "bad": bad}


async def test_the_card_is_stored_when_the_run_settles(db_session: AsyncSession) -> None:
    world = await make_world(db_session, NODES, "b", spec_fields={"card": CARD})
    runs = await _runs(world)
    waiting = await world.get(runs["review"].id)
    assert waiting.status == "waiting", "the run is still going"
    assert waiting.card == {"title": "Q4", "broken": None, "due": "2026-11-05", "step": "1"}, (
        "its card shows what has settled so far"
    )
    done = await world.get(runs["done"].id)
    assert done.card is not None and done.card["step"] is None, "a step that did not run is null"


async def test_the_board_has_a_column_per_stage_then_done_and_failed(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, NODES, "b", spec_fields={"card": CARD})
    runs = await _runs(world)
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=False), NOW)
    shown = await service.runs_view(env, world.workflow_id, "board")
    assert shown.view == "board"
    columns = {c.stage: [x.run.number for x in c.cards] for c in shown.columns}
    assert list(columns) == ["Draft", "Review", "Done", "Failed"], columns
    assert columns == {
        "Draft": [],
        "Review": [runs["review"].number],
        "Done": [runs["done"].number],
        "Failed": [runs["bad"].number],
    }, columns
    (waiting,) = shown.columns[1].cards
    assert waiting.attention and waiting.card["title"] == "Q4", "an approval needs a person"
    assert not shown.columns[2].cards[0].attention, "a finished run does not"


async def test_the_calendar_lists_the_months_runs_by_their_card_date(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, NODES, "b", spec_fields={"card": CARD})
    await _runs(world)
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=False), NOW)
    shown = await service.runs_view(env, world.workflow_id, "calendar", "2026-11")
    assert shown.view == "calendar"
    assert sorted(c.date or "" for c in shown.cards) == ["2026-11-05", "2026-11-20"], shown
    other = await service.runs_view(env, world.workflow_id, "calendar", "2026-12")
    assert [c.date for c in other.cards] == ["2026-12-01"], other
    with pytest.raises(service.Invalid):
        await service.runs_view(env, world.workflow_id, "calendar", "2026-13")


async def test_a_calendar_without_a_date_field_is_empty(db_session: AsyncSession) -> None:
    card = {"fields": CARD["fields"]}
    world = await make_world(db_session, NODES, "b", spec_fields={"card": card})
    await _runs(world)
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner, admin=False), NOW)
    shown = await service.runs_view(env, world.workflow_id, "calendar", "2026-11")
    assert shown.cards == [], "nothing names a day"

"""Blind review through `ReviewService` against Postgres: nothing in a blind task or its
payload names an arm, a model or a model reaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from daimon.core.evals.access import Share, Subject
from daimon.core.evals.bundle import DirSource, to_zip
from daimon.core.evals.format import ScoreValue
from daimon.core.evals.review import (
    CommentDraft,
    Criterion,
    ReviewService,
    ScoreDraft,
    _Column,  # pyright: ignore[reportPrivateUsage]
    _unanswered,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.evals.service import (
    EvalForbidden,
    EvalNotFound,
    EvalService,
    Page,
)
from daimon.core.model_reactions import MODEL_MARKS
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import bare_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SOURCE = DirSource(Path(__file__).parent / "example_bundle")
_REACTION = sorted(MODEL_MARKS)[0]
_SECRET_WORDS = ("Candidate Zed", "candidate-z", "zed-agent", "Baseline A")


def _bundle() -> bytes:
    """The example run with a second arm whose item names its agent and carries a model mark."""
    files = {name: _SOURCE.read(name) for name in _SOURCE.names()}
    base = "runs/baseline-a-2026-01/"
    run = json.loads(files[base + "run.json"])
    run["arms"].append(
        {
            "id": "candidate-z", "label": "Candidate Zed", "kind": "baseline",
            "baseline": {"source_system": "zed-agent"},
        }
    )  # fmt: skip
    first = json.loads(files[base + "items.jsonl"].splitlines()[0])
    second = json.loads(json.dumps(first))
    second["id"] = first["id"] + ".z"
    second["arm_id"] = "candidate-z"
    second["chat"] += [
        {"seq": 2, "kind": "reaction_add", "actor": "300000000000000001", "emoji": _REACTION},
        {"seq": 3, "kind": "message", "actor": "300000000000000001", "message_id": "r2",
         "text": "Reply from zed-agent."},
    ]  # fmt: skip
    third = json.loads(json.dumps(second))
    third.update(id=first["id"] + ".z1", repeat=1)  # a repeat only one arm has
    queues = [
        {"schema": "daimon.eval.review/1", "kind": "queue", "id": q, "name": "Compare",
         "layout": "side_by_side", "blind": blind, "reviewers_per_item": 2}
        for q, blind in (("q2", True), ("q3", False))
    ]  # fmt: skip
    changed = {
        base + "run.json": json.dumps(run).encode(),
        base + "items.jsonl": "\n".join(json.dumps(i) for i in (first, second, third)).encode(),
        base + "reviews.jsonl": "\n".join(json.dumps(q) for q in queues).encode(),
    }
    manifest = json.loads(files["bundle.json"])
    manifest["contents"]["runs"][0]["item_count"] = 3
    for entry in manifest["files"]:
        if entry["path"] in changed:
            data = changed[entry["path"]]
            entry["sha256"], entry["size"] = hashlib.sha256(data).hexdigest(), len(data)
    return to_zip(files | changed | {"bundle.json": json.dumps(manifest).encode()})


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _world(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> tuple[EvalService, ReviewService, Subject, Subject, str]:
    """The example run with two arms, shared with one reviewer; the owner is an admin. Last,
    the id the run got on import."""
    settings = bare_settings().model_copy(update={"features": {"evals": "on"}})
    tenant = await make_tenant(db_session, platform="web", workspace_id="team-a")
    owner = Subject(tenant_id=tenant.id, platform="web", key="web:alex", role="admin")
    reader = Subject(tenant_id=tenant.id, platform="web", key="web:rae", role="member")
    evals = EvalService(db_session_factory, settings)
    [run_id] = (await evals.import_bundle(owner, _chunks(_bundle()))).runs
    await evals.share(
        owner, "support-basics", [Share(kind="person", id="web:rae", role="reviewer")]
    )
    return evals, ReviewService(db_session_factory, evals), owner, reader, run_id


async def test_blind_task_when_served_then_payload_names_no_arm_model_or_reaction(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    evals, review, _, reader, run_id = await _world(db_session, db_session_factory)

    task = await review.next(reader, f"{run_id}~q2")
    assert task is not None and len(task.columns) == 2, "both arms come as columns"
    payload = task.model_dump_json(by_alias=True)
    for word in (*_SECRET_WORDS, _REACTION):
        assert word not in payload, f"the task leaks {word!r}"
    assert sorted(c.label for c in task.columns) == ["A", "B"], "columns carry neutral letters"
    assert all(c.arm is None for c in task.columns), "no column names its arm"

    assert all(run_id not in c.item.id for c in task.columns), "item ids are opaque"
    comments = await review.task_comments(reader, task.id)
    assert comments == [], "a fresh task has no comments"

    shown = await evals.items(reader, run_id, page=Page(offset=0, limit=50))
    assert not {i.arm_id for i in shown} & {"baseline-a", "candidate-z"}, (
        "the item list is blind too"
    )

    drafts = [ScoreDraft(criterion="preference", value=ScoreValue(preference="a"))]
    answers = [
        ScoreDraft(criterion=c.id, target=col.label, value=ScoreValue(pass_=True))
        for c in task.criteria
        if c.kind != "preference"
        for col in task.columns
    ]
    await review.submit(reader, task.id, [*answers, *drafts])
    assert await review.next(reader, f"{run_id}~q2") is None, "the one task is done"


async def test_blind_comment_when_made_or_listed_then_it_names_no_author(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, review, _, reader = await _world(db_session, db_session_factory)
    task = await review.next(reader, f"{_RUN}~q2")
    assert task is not None
    made = await review.comment(
        reader, CommentDraft(text="Looks fine.", task_id=task.id, target="A")
    )
    [listed] = await review.task_comments(reader, task.id)
    for comment in (made, listed):
        payload = comment.model_dump_json(by_alias=True)
        assert "web:rae" not in payload and _RUN not in payload, "no author or item id"
        assert comment.author.display_name and comment.author.display_name.startswith("Reviewer")
    assert made.author == listed.author, "one reviewer keeps one alias in the task"


async def test_reviewer_when_queue_is_not_blind_then_the_task_is_blind_anyway(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, review, owner, reader, run_id = await _world(db_session, db_session_factory)
    task = await review.next(reader, f"{run_id}~q3")
    assert task is not None and task.blind, "a reviewer's task is blind whatever the queue says"
    assert all(c.arm is None and c.item.usage is None for c in task.columns)
    assert not any(w in task.model_dump_json(by_alias=True) for w in _SECRET_WORDS)
    seen = await review.next(owner, f"{run_id}~q3")
    assert seen is not None and not seen.blind, "an admin still sees the visible queue"


async def test_reviewer_when_listing_items_then_only_matched_finished_ones_with_opaque_ids(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    evals, _, owner, reader, run_id = await _world(db_session, db_session_factory)
    page = Page(offset=0, limit=50)
    shown = await evals.items(reader, run_id, page=page)
    assert len(shown) == 2, "the repeat only one arm has is not listed"
    assert all(i.repeat == 0 and i.case_revision == 0 and i.id == i.key for i in shown)
    everything = await evals.items(owner, run_id, page=page)
    assert len(everything) == 3, "an editor sees every item"
    assert not {i.id for i in shown} & {i.id for i in everything}, "no row id is shared"

    item = await evals.item(reader, shown[0].id)
    assert item.arm_id == shown[0].arm_id and item.error is None and item.usage is None
    with pytest.raises(EvalNotFound):  # last: a refusal rolls the shared test connection back
        await evals.item(reader, everything[0].id)


async def test_arms_and_cases_when_a_reviewer_asks_then_arms_are_refused_and_cases_trimmed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    evals, _, owner, reader, run_id = await _world(db_session, db_session_factory)
    with pytest.raises(EvalForbidden):
        await evals.arms(reader)
    assert isinstance(await evals.arms(owner), list), "admins list the arms"
    [case_ref, *_] = await evals.cases(owner, "support-basics")
    seen = await evals.case(reader, "support-basics", case_ref.id)
    assert set(seen) <= {"id", "title", "platform", "history", "turn", "reference", "participants"}
    assert seen["participants"], "authors come with their display names"
    assert "world" in await evals.case(owner, "support-basics", case_ref.id)


def test_unanswered_when_a_question_is_left_out_then_it_is_named() -> None:
    specs = {
        "overall": Criterion(id="overall", question="?", kind="overall"),
        "preference": Criterion(id="preference", question="?", kind="preference"),
    }
    cols = [_Column(n, None, None, n) for n in "AB"]  # pyright: ignore[reportArgumentType]
    pref = ScoreDraft(criterion="preference", value=ScoreValue(preference="a"))
    overall = [
        ScoreDraft(criterion="overall", target=n, value=ScoreValue(pass_=True)) for n in "AB"
    ]
    assert _unanswered(specs, cols, [pref]) == ["overall A", "overall B"]
    assert _unanswered(specs, cols, [*overall[:1], pref]) == ["overall B"]
    assert _unanswered(specs, cols, [*overall, pref]) == [], "every question answered"

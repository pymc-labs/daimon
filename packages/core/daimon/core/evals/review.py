"""Review: queues, reservations, blind tasks, human scores, adjudication and comments.

A person or an agent works through a queue one task at a time. A task is one item (`single`)
or the same case and repeat from several arms (`side_by_side`). Columns are named by their
place in the task (A, B...). In a blind queue the server shows items through the blind item
view, orders the columns randomly per task and reviewer, serves only repeats every arm has,
and keeps task ids opaque, so every surface that serves a task (web, MCP, CLI) is blind the
same way. There is no skip: a task left alone is offered again once its reservation ends.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Final, Literal

from daimon.core.evals.access import Subject
from daimon.core.evals.blind import (
    arm_labels,
    arm_names,
    blind_item_view,
    handle,
    is_blind,
    mask_document,
)
from daimon.core.evals.format import (
    Anchor,
    Arm,
    Case,
    ChatMessage,
    Comment,
    Grader,
    ItemPair,
    JsonObject,
    PrincipalRef,
    Reference,
    ReviewQueue,
    RubricScale,
    Run,
    RunItem,
    Score,
    ScoreValue,
    to_document,
)
from daimon.core.evals.scoring.judges import orient
from daimon.core.evals.service import (
    EvalForbidden,
    EvalNotFound,
    EvalRefused,
    EvalService,
    with_transcript,
)
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEASE: Final = timedelta(minutes=30)
"""How long a task stays reserved for the reviewer it was handed to."""
_SEP: Final = "~"
_WHO: Final[dict[str, Literal["person", "agent"]]] = {"human": "person", "agent": "agent"}
Layout = Literal["single", "side_by_side"]
By = Literal["human", "agent"]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QueueDraft(_Out):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    name: str
    layout: Layout = "side_by_side"
    blind: bool = True
    arms: list[str] | None = None
    criteria: list[str] | None = None
    """Rubric ids, `overall` and `preference`. None: the case's human rubric, `overall`, and
    `preference` for side by side."""
    reviewers_per_item: int = Field(default=1, ge=1)
    reviewers: list[PrincipalRef] | None = None
    """Who may review. None: anyone with the reviewer role on the suite."""
    status: Literal["open", "closed"] = "open"


class QueueView(_Out):
    ref: str
    """`run~queue`: what `next` takes."""
    run_id: str
    queue: ReviewQueue
    """Arms by their neutral labels for a blind reader."""
    total: int
    done: int
    """Tasks finished by enough reviewers."""


class Criterion(_Out):
    id: str
    question: str
    kind: Literal["binary", "scale", "overall", "preference"]
    scale: RubricScale | None = None


class CaseView(_Out):
    """The part of a case a reviewer needs; never its world, provenance or expectations."""

    id: str
    title: str
    platform: str
    history: list[ChatMessage]
    turn: list[ChatMessage]
    reference: Reference | None


class TaskColumn(_Out):
    label: str
    """The column's place in the task, `A`, `B`...: what `ScoreDraft.target`,
    `CommentDraft.target` and anchors name it by."""
    arm: Arm | None
    """Only in a visible queue."""
    item: RunItem
    """In a blind queue, the blind item view: `id` is the opaque id the item view takes."""


class ReviewTask(_Out):
    id: str
    """What `submit`, `comment` and `task_comments` take."""
    queue: str
    layout: Layout
    blind: bool
    case: CaseView
    criteria: list[Criterion]
    """`overall` and rubric items are answered per column (`target`); `preference` once."""
    columns: list[TaskColumn]
    comments: list[Comment]
    """On this task's items; in a blind queue only those made in this queue."""
    reserved_until: datetime | None
    done: int
    """Tasks of this queue the reviewer finished."""
    total: int


class ScoreDraft(_Out):
    criterion: str
    value: ScoreValue
    target: str | None = None
    """The column a per-item score is about; none for a single item or a preference."""
    rationale: str | None = None
    anchors: list[Anchor] | None = None
    """Anchors name items by column label; the server writes the real ids."""
    supersedes: str | None = None
    """Adjudication (editors and owners): the earlier human score this one replaces."""


class CommentDraft(_Out):
    text: str = Field(min_length=1)
    task_id: str | None = None
    """Comment from inside a review task: `target` names the column; without it the comment
    is on the case. Reviewers comment only this way."""
    target: str | None = None
    run_id: str | None = None
    item_id: str | None = None
    case_id: str | None = None
    anchors: list[Anchor] | None = None
    parent_id: str | None = None


class Disagreement(_Out):
    task_id: str
    criterion: str
    target: str | None
    scores: list[JsonObject]


@dataclass(frozen=True)
class _Unit:
    key: str
    """Opaque: names no item, arm or repeat."""
    case_key: str
    repeat: int
    items: tuple[str, ...]


def _units(
    queue: ReviewQueue, salt: str, run_arms: Sequence[str], index: Sequence[store.ItemIndexRow]
) -> list[_Unit]:
    """The queue's tasks. A blind queue takes only repeats every arm it covers finished, so
    which repeats exist never tells live arms from recorded ones."""
    arms = set(queue.arms or run_arms)
    live = [i for i in index if i.status == "done" and i.arm_key in arms]
    groups: dict[tuple[str, int], list[store.ItemIndexRow]] = defaultdict(list)
    for i in live:
        groups[(i.case_key, i.repeat)].append(i)
    if queue.blind:
        groups = {k: g for k, g in groups.items() if {i.arm_key for i in g} == arms}
    if queue.layout == "single":
        return [
            _Unit(handle(salt, i.key), i.case_key, i.repeat, (i.key,))
            for g in groups.values()
            for i in g
        ]
    return [
        _Unit(handle(salt, f"{case}{_SEP}{repeat}"), case, repeat, tuple(i.key for i in g))
        for (case, repeat), g in groups.items()
        if len(g) >= 2
    ]


def _comment_prefix(queue_ref: str) -> str:
    """Comments made in a queue carry this in their id, so a blind task shows only those."""
    return f"c-{handle(queue_ref, 'comments')[2:14]}-"


def _roll(*parts: str) -> random.Random:
    return random.Random(int(sha256("|".join(parts).encode()).hexdigest(), 16))


def _criteria(queue: ReviewQueue, case: Case, columns: int) -> list[Criterion]:
    rubric = {r.id: r for r in case.expect.rubric or []}
    wanted = queue.criteria or [
        *(i for i, r in rubric.items() if "human" in r.graders),
        "overall",
        *(["preference"] if queue.layout == "side_by_side" and columns == 2 else []),
    ]
    found: list[Criterion] = []
    for criterion in wanted:
        if criterion in rubric:
            r = rubric[criterion]
            kind = "scale" if r.kind == "scale" else "binary"
            found.append(Criterion(id=r.id, question=r.criterion, kind=kind, scale=r.scale))
        elif criterion == "overall":
            found.append(
                Criterion(id="overall", question="Is this answer acceptable?", kind="overall")
            )
        elif criterion == "preference" and columns == 2:
            found.append(
                Criterion(id="preference", question="Which answer is better?", kind="preference")
            )
    return found


def _check_value(criterion: Criterion, value: ScoreValue) -> str | None:
    """Why a score's value does not fit its criterion; None when it does."""
    if value.unknown:
        return None
    if criterion.kind == "preference":
        return None if value.preference is not None else "Give a, b or tie."
    if criterion.kind == "scale":
        scale = criterion.scale
        ok = value.score is not None and (scale is None or scale.min <= value.score <= scale.max)
        return None if ok else "Give a score inside the scale."
    return None if value.pass_ is not None else "Give pass or fail."


def _ref(run: str, queue: str) -> str:
    return f"{run}{_SEP}{queue}"


def _split(ref: str, parts: int) -> list[str]:
    found = ref.split(_SEP, parts - 1)
    if len(found) != parts or not all(found):
        raise EvalNotFound("No such queue or task here.")
    return found


@dataclass(frozen=True)
class _Column:
    label: str
    item: RunItem
    arm: Arm
    opaque_id: str
    """The item's row id: what the blind item view and blind anchors use."""


def _reviewer(actor: Subject, by: By) -> str:
    """Whose slot a task takes: an agent working for a person has its own."""
    return actor.key if by == "human" else f"{actor.key}#agent"


class ReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        evals: EvalService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._evals = evals
        self._clock = clock

    # ---------- queues ----------

    async def create_queue(self, actor: Subject, run_id: str, draft: QueueDraft) -> QueueView:
        """Make a queue on a run, or change the settings of one with the same id. Editors."""
        await self._evals.check_on(actor)
        async with self._sessions() as session, session.begin():
            run, _ = await self._evals.run_access(session, actor, run_id, "edit")
            shown = Run.model_validate(run.document)
            unknown = [a for a in draft.arms or [] if a not in {x.id for x in shown.arms}]
            if unknown:
                raise EvalRefused(f"The run has no arm {', '.join(unknown)}.")
            queue = ReviewQueue(
                id=draft.id, name=draft.name, layout=draft.layout, blind=draft.blind,
                arms=draft.arms, criteria=draft.criteria,
                reviewers_per_item=draft.reviewers_per_item,
                reviewers=draft.reviewers, status=draft.status,
            )  # fmt: skip
            row = await store.put_queue(
                session, tenant_id=actor.tenant_id, run_id=run.id, document=to_document(queue)
            )
            return await self._view(session, run, row, blind=False)

    async def queues(self, actor: Subject, run_id: str) -> list[QueueView]:
        """The run's queues the actor may work or manage; a reviewer sees arms as labels."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            run, role = await self._evals.run_access(session, actor, run_id, "review")
            views: list[QueueView] = []
            for row in await store.queues_of_run(session, run_id=run.id):
                queue = ReviewQueue.model_validate(row.document)
                if role in ("editor", "owner") or await self._may_review(actor, queue):
                    views.append(await self._view(session, run, row, blind=is_blind(role)))
            return views

    async def my_queues(self, actor: Subject) -> list[QueueView]:
        """Open queues across the workspace that the actor may review, as a reviewer sees them:
        every run they review, and queues that name them or a group they are in."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            views: list[QueueView] = []
            roles: dict[str, str | None] = {}
            for run, row in await store.queues_of_tenant(session, tenant_id=actor.tenant_id):
                queue = ReviewQueue.model_validate(row.document)
                if queue.status == "closed" or not await self._may_review(actor, queue):
                    continue
                if run.key not in roles:
                    try:
                        roles[run.key] = (
                            await self._evals.run_access(session, actor, run.key, "review")
                        )[1]
                    except EvalRefused:
                        roles[run.key] = None
                role = roles[run.key]
                if role is not None:
                    views.append(await self._view(session, run, row, blind=is_blind(role)))
            return views

    async def _may_review(self, actor: Subject, queue: ReviewQueue) -> bool:
        if not queue.reviewers:
            return True
        if any(r.kind != "group" and r.id in actor.ids for r in queue.reviewers):
            return True
        wanted = frozenset(r.id for r in queue.reviewers if r.kind == "group")
        return bool(wanted and await actor.groups(wanted))

    async def _view(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, *, blind: bool
    ) -> QueueView:
        queue = ReviewQueue.model_validate(row.document)
        shown = Run.model_validate(run.document)
        ref = _ref(run.key, row.key)
        index = await store.item_index(session, run_id=run.id)
        units = _units(queue, ref, [a.id for a in shown.arms], index)
        need = queue.reviewers_per_item or 1
        done: dict[str, int] = defaultdict(int)
        for a in await store.assignments_of(session, queue_id=row.id):
            done[a.item_key] += a.done_at is not None
        if blind and queue.arms:
            labels = arm_labels(shown)
            queue = queue.model_copy(update={"arms": [labels.get(a, "Arm") for a in queue.arms]})
        return QueueView(
            ref=ref, run_id=run.key, queue=queue, total=len(units),
            done=sum(done[u.key] >= need for u in units),
        )  # fmt: skip

    # ---------- tasks ----------

    async def next(self, actor: Subject, queue_ref: str, *, by: By = "human") -> ReviewTask | None:
        """The reviewer's current task, or the next one with a free slot; None when none is left."""
        await self._evals.check_on(actor)
        run_key, queue_key = _split(queue_ref, 2)
        reviewer = _reviewer(actor, by)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            run, _ = await self._evals.run_access(session, actor, run_key, "review")
            row = await store.queue_by_key(session, run_id=run.id, key=queue_key, lock=True)
            if row is None:
                raise EvalNotFound("No such queue or task here.")
            queue = ReviewQueue.model_validate(row.document)
            if queue.status == "closed" or not await self._may_review(actor, queue):
                raise EvalForbidden("This queue is closed or not yours to review.")
            units = await self._all_units(session, run, row, queue)
            assigned = await store.assignments_of(session, queue_id=row.id)
            need = queue.reviewers_per_item or 1
            mine = {a.item_key: a for a in assigned if a.reviewer_key == reviewer}
            taken: dict[str, int] = defaultdict(int)
            for a in assigned:
                live = a.reserved_until is not None and a.reserved_until > now
                taken[a.item_key] += a.done_at is not None or (live and a.reviewer_key != reviewer)
            held = [u for u in units if _open_lease(mine.get(u.key), now)]
            free = [u for u in units if u.key not in mine and taken[u.key] < need]
            stale = [u for u in units if _lapsed(mine.get(u.key), now) and taken[u.key] < need]
            chosen = next(iter(held or free or stale), None)
            if chosen is None:
                return None
            until = now + LEASE
            await store.reserve_item(
                session, tenant_id=actor.tenant_id, queue_id=row.id, item_key=chosen.key,
                reviewer_key=reviewer, until=until,
            )  # fmt: skip
            done_by_me = sum(1 for a in mine.values() if a.done_at is not None)
            columns = await self._columns(session, actor, run, row, queue, chosen, reviewer)
            case = await self._case(session, run, chosen.case_key)
            comments = await self._task_comments(session, run, row, queue, columns)
        return self._task(
            run, row, queue, chosen, columns, case, comments, until=until,
            progress=(done_by_me, len(units)),
        )  # fmt: skip

    async def _all_units(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, queue: ReviewQueue
    ) -> list[_Unit]:
        arms = [a.id for a in Run.model_validate(run.document).arms]
        index = await store.item_index(session, run_id=run.id)
        return _units(queue, _ref(run.key, row.key), arms, index)

    async def _unit(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, queue: ReviewQueue,
        key: str,
    ) -> _Unit:  # fmt: skip
        for unit in await self._all_units(session, run, row, queue):
            if unit.key == key:
                return unit
        raise EvalNotFound("No such queue or task here.")

    async def _columns(
        self, session: AsyncSession, actor: Subject, run: store.RunRow, row: store.QueueRow,
        queue: ReviewQueue, unit: _Unit, reviewer: str,
    ) -> list[_Column]:  # fmt: skip
        """The unit's items as displayed, named A, B... by place; a blind queue shuffles them
        per reviewer and shows each through the blind item view."""
        shown = Run.model_validate(run.document)
        rows = await store.items_by_key(session, run_id=run.id, keys=unit.items)
        found = sorted((rows[k] for k in unit.items if k in rows), key=lambda r: r.arm_key)
        if queue.blind:
            _roll(_ref(run.key, row.key), unit.key, reviewer).shuffle(found)
        arms = {a.id: a for a in shown.arms}
        columns: list[_Column] = []
        for n, r in enumerate(found):
            item = RunItem.model_validate(r.document)
            if queue.blind:
                item = await with_transcript(item, partial(self._evals.blob_bytes, actor.tenant_id))
            columns.append(_Column(chr(65 + n), item, arms[item.arm_id], str(r.id)))
        return columns

    def _task(
        self, run: store.RunRow, row: store.QueueRow, queue: ReviewQueue, unit: _Unit,
        columns: Sequence[_Column], case: Case, comments: list[Comment], *,
        until: datetime | None, progress: tuple[int, int],
    ) -> ReviewTask:  # fmt: skip
        names = arm_names(Run.model_validate(run.document))
        out: list[TaskColumn] = []
        for c in columns:
            if queue.blind:
                view = blind_item_view(c.item, item_id=c.opaque_id, label=c.label, names=names)
                out.append(TaskColumn(label=c.label, arm=None, item=RunItem.model_validate(view)))
            else:
                out.append(TaskColumn(label=c.label, arm=c.arm, item=c.item))
        ref = _ref(run.key, row.key)
        return ReviewTask(
            id=f"{ref}{_SEP}{unit.key}", queue=ref, layout=queue.layout, blind=queue.blind,
            case=_case_view(case, names if queue.blind else None),
            criteria=_criteria(queue, case, len(columns)), columns=out, comments=comments,
            reserved_until=until, done=progress[0], total=progress[1],
        )  # fmt: skip

    async def _case(self, session: AsyncSession, run: store.RunRow, case_key: str) -> Case:
        rows = await store.version_cases(session, suite_id=run.suite_id, version=run.suite_version)
        for r in rows:
            if r.key == case_key:
                return Case.model_validate(r.document)
        raise EvalNotFound("The case of this task is gone.")

    async def _task_comments(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, queue: ReviewQueue,
        columns: Sequence[_Column],
    ) -> list[Comment]:  # fmt: skip
        """Comments on the task's items or case; a blind queue shows only its own, by
        opaque item ids and with model names masked."""
        ids = {c.item.id: c.opaque_id for c in columns}
        case_id = columns[0].item.case.id if columns else None
        prefix = _comment_prefix(_ref(run.key, row.key))
        names = arm_names(Run.model_validate(run.document))
        found: list[Comment] = []
        for r in await store.comments_of_run(session, run_id=run.id):
            comment = Comment.model_validate(r.document)
            if comment.item_id not in ids and not (
                comment.item_id is None and comment.case_id == case_id
            ):
                continue
            if not queue.blind:
                found.append(comment)
            elif comment.id.startswith(prefix):
                found.append(_hide(comment, ids, names))
        return found

    async def task_comments(
        self, actor: Subject, task_id: str, *, by: By = "human"
    ) -> list[Comment]:
        """The comments of a review task, as the task shows them."""
        await self._evals.check_on(actor)
        run_key, queue_key, unit_key = _split(task_id, 3)
        async with self._sessions() as session:
            run, _ = await self._evals.run_access(session, actor, run_key, "review")
            row, queue = await self._queue(session, run, queue_key)
            unit = await self._unit(session, run, row, queue, unit_key)
            columns = await self._columns(
                session, actor, run, row, queue, unit, _reviewer(actor, by)
            )
            return await self._task_comments(session, run, row, queue, columns)

    async def _queue(
        self, session: AsyncSession, run: store.RunRow, key: str, *, lock: bool = False
    ) -> tuple[store.QueueRow, ReviewQueue]:
        row = await store.queue_by_key(session, run_id=run.id, key=key, lock=lock)
        if row is None:
            raise EvalNotFound("No such queue or task here.")
        return row, ReviewQueue.model_validate(row.document)

    # ---------- scores ----------

    async def submit(
        self, actor: Subject, task_id: str, scores: Sequence[ScoreDraft], *, by: By = "human"
    ) -> None:
        """Record a reviewer's scores for their task, and close it for them.

        Editors and owners can also adjudicate: drafts that all name `supersedes` replace
        earlier human scores and need no task of their own.
        """
        await self._evals.check_on(actor)
        if not scores:
            raise EvalRefused("Give at least one score.")
        run_key, queue_key, unit_key = _split(task_id, 3)
        reviewer = _reviewer(actor, by)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            run, role = await self._evals.run_access(session, actor, run_key, "review")
            row, queue = await self._queue(session, run, queue_key, lock=True)
            if all(d.supersedes for d in scores):
                await self._adjudicate(session, actor, role, run, scores, now)
                return
            unit = await self._unit(session, run, row, queue, unit_key)
            held = [
                a
                for a in await store.assignments_of(session, queue_id=row.id)
                if a.item_key == unit_key and a.reviewer_key == reviewer
            ]
            if not held or not _open_lease(held[0], now):
                raise EvalForbidden(
                    "This task is not reserved for you now. Get it again from the queue."
                )
            columns = await self._columns(session, actor, run, row, queue, unit, reviewer)
            case = await self._case(session, run, unit.case_key)
            specs = {c.id: c for c in _criteria(queue, case, len(columns))}
            made = [self._score(actor, by, queue, d, specs, columns, now) for d in scores]
            await store.put_scores(session, tenant_id=actor.tenant_id, run_id=run.id, scores=made)
            await store.finish_item(
                session, queue_id=row.id, item_key=unit_key, reviewer_key=reviewer, at=now
            )

    def _score(
        self, actor: Subject, by: By, queue: ReviewQueue, draft: ScoreDraft,
        specs: Mapping[str, Criterion], columns: Sequence[_Column], now: datetime,
    ) -> JsonObject:  # fmt: skip
        spec = specs.get(draft.criterion)
        if spec is None or draft.supersedes:
            raise EvalRefused(f"This task does not ask for {draft.criterion}.")
        if problem := _check_value(spec, draft.value):
            raise EvalRefused(f"{draft.criterion}: {problem}")
        by_label = {c.label: c for c in columns}
        item_id: str | None = None
        pair: ItemPair | None = None
        if spec.kind == "preference":
            pair = ItemPair(a=columns[0].item.id, b=columns[1].item.id)  # A and B, as shown
        else:
            target = draft.target or (columns[0].label if len(columns) == 1 else None)
            if target not in by_label:
                raise EvalRefused(f"{draft.criterion}: name the column (A, B...) it is about.")
            item_id = by_label[target].item.id
        anchors = [_anchor(a, columns) for a in draft.anchors or []]
        score = Score(
            id=f"h-{uuid.uuid4().hex}", item_id=item_id, criterion=draft.criterion,
            pair=pair, grader=Grader(kind=by, who=PrincipalRef(kind=_WHO[by], id=actor.key)),
            value=draft.value, rationale=draft.rationale, anchors=anchors or None,
            blind=queue.blind, at=now,
        )  # fmt: skip
        return to_document(score)

    async def _adjudicate(
        self, session: AsyncSession, actor: Subject, role: str, run: store.RunRow,
        drafts: Sequence[ScoreDraft], now: datetime,
    ) -> None:  # fmt: skip
        if role not in ("editor", "owner") and not actor.is_admin:
            raise EvalForbidden("Only editors and owners can adjudicate.")
        existing = {str(d["id"]): d for d in await store.scores_of_run(session, run_id=run.id)}
        made: list[JsonObject] = []
        for draft in drafts:
            old = existing.get(draft.supersedes or "")
            if old is None or _kind(old) != "human":
                raise EvalRefused("A decision can only replace a human score of this run.")
            prior = Score.model_validate(old)
            grader = prior.grader.model_copy(
                update={"who": PrincipalRef(kind="person", id=actor.key)}
            )
            score = prior.model_copy(
                update={
                    "id": f"h-{uuid.uuid4().hex}", "value": draft.value,
                    "rationale": draft.rationale, "grader": grader,
                    "supersedes": draft.supersedes, "at": now, "anchors": None,
                }
            )  # fmt: skip
            made.append(to_document(score))
        await store.put_scores(session, tenant_id=actor.tenant_id, run_id=run.id, scores=made)

    async def disagreements(self, actor: Subject, queue_ref: str) -> list[Disagreement]:
        """Criteria on which reviewers of one task gave different answers, for adjudication.
        Preferences given in either display order are compared as one pair."""
        await self._evals.check_on(actor)
        run_key, queue_key = _split(queue_ref, 2)
        async with self._sessions() as session:
            run, _ = await self._evals.run_access(session, actor, run_key, "edit")
            row, queue = await self._queue(session, run, queue_key)
            units = await self._all_units(session, run, row, queue)
            raw = await store.scores_of_run(session, run_id=run.id)
        replaced = {str(d["supersedes"]) for d in raw if d.get("supersedes")}
        humans = [
            orient(Score.model_validate(d), str)
            for d in raw
            if _kind(d) == "human" and d["id"] not in replaced
        ]
        found: list[Disagreement] = []
        for unit in units:
            keys = set(unit.items)
            groups: dict[str, list[Score]] = defaultdict(list)
            for s in humans:
                if s.item_id in keys or (s.pair is not None and s.pair.a in keys):
                    on = s.item_id or (f"{s.pair.a}|{s.pair.b}" if s.pair else "")
                    groups[f"{s.criterion}|{on}"].append(s)
            for group in groups.values():
                values = {
                    s.value.model_dump_json(by_alias=True) for s in group if not s.value.unknown
                }
                if len(values) > 1:
                    found.append(
                        Disagreement(
                            task_id=f"{_ref(run.key, row.key)}{_SEP}{unit.key}",
                            criterion=group[0].criterion, target=None,
                            scores=[_hide_score(to_document(s)) for s in group],
                        )
                    )  # fmt: skip
        return found

    # ---------- comments ----------

    async def comment(self, actor: Subject, draft: CommentDraft, *, by: By = "human") -> Comment:
        """Add a comment on an item or a case, or from inside a review task by column.
        Reviewers see runs blind, so they comment only from inside a task."""
        await self._evals.check_on(actor)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            if draft.task_id:
                run_key, queue_key, unit_key = _split(draft.task_id, 3)
            elif draft.run_id:
                run_key, queue_key, unit_key = draft.run_id, "", ""
            else:
                raise EvalRefused("Name a task or a run to comment on.")
            run, role = await self._evals.run_access(session, actor, run_key, "review")
            item_id, case_id, anchors = draft.item_id, draft.case_id, draft.anchors
            key = f"c-{uuid.uuid4().hex}"
            if draft.task_id:
                row, queue = await self._queue(session, run, queue_key)
                unit = await self._unit(session, run, row, queue, unit_key)
                columns = await self._columns(
                    session, actor, run, row, queue, unit, _reviewer(actor, by)
                )
                by_label = {c.label: c for c in columns}
                if draft.target is not None and draft.target not in by_label:
                    raise EvalRefused("Name a column of this task.")
                item_id = by_label[draft.target].item.id if draft.target else None
                case_id = None if item_id else unit.case_key
                anchors = [_anchor(a, columns) for a in draft.anchors or []] or None
                key = f"{_comment_prefix(_ref(run.key, row.key))}{uuid.uuid4().hex}"
            elif is_blind(role):
                raise EvalForbidden("Reviewers comment from inside a review task.")
            if item_id is None and case_id is None:
                raise EvalRefused("A comment is on an item or a case.")
            comment = Comment(
                id=key, item_id=item_id, case_id=case_id, parent_id=draft.parent_id,
                author=PrincipalRef(kind=_WHO[by], id=actor.key), text=draft.text,
                anchors=anchors, at=now,
            )  # fmt: skip
            await store.add_comment(
                session, tenant_id=actor.tenant_id, run_id=run.id, suite_id=run.suite_id,
                document=to_document(comment),
            )  # fmt: skip
        return comment

    async def comments(self, actor: Subject, run_id: str) -> list[Comment]:
        """Every comment on a run; reviewers get theirs with each task instead."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            run, role = await self._evals.run_access(session, actor, run_id, "view")
            if is_blind(role):
                raise EvalForbidden("Comments come with each review task.")
            rows = await store.comments_of_run(session, run_id=run.id)
        return [Comment.model_validate(r.document) for r in rows]


def _open_lease(held: store.AssignmentRow | None, now: datetime) -> bool:
    return bool(held and held.done_at is None and held.reserved_until and held.reserved_until > now)


def _lapsed(held: store.AssignmentRow | None, now: datetime) -> bool:
    """A task this reviewer reserved and let lapse: offered to them again."""
    return bool(held and held.done_at is None and not _open_lease(held, now))


def _anchor(anchor: Anchor, columns: Sequence[_Column]) -> Anchor:
    """An anchor naming a column by label or opaque id, with the item's real id."""
    for c in columns:
        if anchor.item_id in (c.label, c.opaque_id):
            return anchor.model_copy(update={"item_id": c.item.id})
    raise EvalRefused("An anchor names a column that is not in this task.")


def _case_view(case: Case, names: frozenset[str] | None) -> CaseView:
    view = CaseView(
        id=case.id, title=case.title, platform=case.surface.platform, history=case.history,
        turn=case.turn.messages, reference=case.expect.reference,
    )  # fmt: skip
    if names is None:
        return view
    masked = mask_document(view.model_dump(mode="json", by_alias=True), names)
    return CaseView.model_validate(masked)


def _hide(comment: Comment, ids: Mapping[str, str], names: frozenset[str]) -> Comment:
    """A comment for a blind reader: item ids as the items' opaque ids, no model names."""
    update: dict[str, object] = {}
    if comment.item_id:
        update["item_id"] = ids.get(comment.item_id, "")
    if comment.anchors:
        update["anchors"] = [
            a.model_copy(update={"item_id": ids.get(a.item_id, "")}) for a in comment.anchors
        ]
    masked = mask_document(to_document(comment.model_copy(update=update)), names)
    return Comment.model_validate(masked)


def _kind(document: JsonObject) -> str:
    grader = document.get("grader")
    return str(grader.get("kind")) if isinstance(grader, dict) else ""


def _hide_score(document: JsonObject) -> JsonObject:
    """A score without the item ids that could name an arm."""
    return {k: v for k, v in document.items() if k not in ("item_id", "pair", "anchors")}

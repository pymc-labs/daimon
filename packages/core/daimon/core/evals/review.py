"""Review: queues, reservations, blind tasks, human scores, adjudication and comments.

A person or an agent works through a queue one task at a time. A task is one item (`single`)
or the same case and repeat from several arms (`side_by_side`). In a blind queue the server
swaps arm names for labels, hides model names and usage, and orders the columns randomly per
task, so every surface that serves a task (web, MCP, CLI) is blind the same way.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final, Literal

from daimon.core.config import Settings
from daimon.core.evals.access import Subject
from daimon.core.evals.blind import (
    arm_names,
    blind_item_view,
    mask_document,
    neutral_labels,
)
from daimon.core.evals.blind import handle as blind_handle
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
from daimon.core.evals.service import EvalForbidden, EvalNotFound, EvalRefused, EvalService
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEASE: Final = timedelta(minutes=30)
"""How long a task stays reserved for the reviewer it was handed to."""
_SEP: Final = "~"
_WHO: Final[dict[str, Literal["person", "agent"]]] = {"human": "person", "agent": "agent"}
Layout = Literal["single", "side_by_side"]


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
    queue: ReviewQueue
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
    """What scores and comments name this column by: `A`, `B`..."""
    arm: Arm | None
    """Only in a visible queue."""
    item: RunItem


class ReviewTask(_Out):
    id: str
    queue: str
    layout: Layout
    blind: bool
    case: CaseView
    criteria: list[Criterion]
    columns: list[TaskColumn]
    comments: list[Comment]
    reserved_until: datetime | None
    done: int
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
    """Comment from inside a review task: `target` names the column."""
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
    case_key: str
    repeat: int
    items: tuple[str, ...]


def _units(
    layout: Layout, arms: Sequence[str] | None, index: Sequence[store.ItemIndexRow]
) -> list[_Unit]:
    live = [i for i in index if i.status == "done" and (arms is None or i.arm_key in arms)]
    if layout == "single":
        return [_Unit(i.key, i.case_key, i.repeat, (i.key,)) for i in live]
    groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    for i in live:
        groups[(i.case_key, i.repeat)].append(i.key)
    return [
        _Unit(f"{case}{_SEP}{repeat}", case, repeat, tuple(keys))
        for (case, repeat), keys in groups.items()
        if len(keys) >= 2
    ]


def _roll(*parts: str) -> random.Random:
    return random.Random(int(sha256("|".join(parts).encode()).hexdigest(), 16))


def _criteria(queue: ReviewQueue, case: Case, columns: int) -> list[Criterion]:
    rubric = {r.id: r for r in case.expect.rubric or []}
    wanted = queue.criteria or [
        *(i for i, r in rubric.items() if "human" in r.graders),
        "overall",
        *(["preference"] if queue.layout == "side_by_side" and columns > 1 else []),
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
        elif criterion == "preference" and columns > 1:
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


class ReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._evals = EvalService(sessions, settings)
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
            return await self._view(session, run, row, blind_arms=False)

    async def queues(self, actor: Subject, run_id: str) -> list[QueueView]:
        """The run's queues the actor may work or manage; a blind reviewer sees arms as labels."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            run, role = await self._evals.run_access(session, actor, run_id, "review")
            rows = await store.queues_of_run(session, run_id=run.id)
            views: list[QueueView] = []
            for row in rows:
                queue = ReviewQueue.model_validate(row.document)
                if role != "editor" and role != "owner" and not self._may_review(actor, queue):
                    continue
                views.append(
                    await self._view(
                        session,
                        run,
                        row,
                        blind_arms=await self._evals.blind_required(session, actor, role, run),
                    )
                )
            return views

    def _may_review(self, actor: Subject, queue: ReviewQueue) -> bool:
        return not queue.reviewers or any(r.id in actor.ids for r in queue.reviewers)

    async def _view(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, *, blind_arms: bool
    ) -> QueueView:
        queue = ReviewQueue.model_validate(row.document)
        index = await store.item_index(session, run_id=run.id)
        units = _units(queue.layout, queue.arms, index)
        need = queue.reviewers_per_item or 1
        done: dict[str, int] = defaultdict(int)
        for a in await store.assignments_of(session, queue_id=row.id):
            done[a.item_key] += a.done_at is not None
        if blind_arms and queue.arms:
            shown = Run.model_validate(run.document)
            labels = neutral_labels(
                [a.id for a in shown.arms], _ref(run.key, row.key), shown.blind_labels
            )
            queue = queue.model_copy(update={"arms": [labels.get(a, "arm") for a in queue.arms]})
        return QueueView(
            ref=_ref(run.key, row.key), queue=queue, total=len(units),
            done=sum(done[u.key] >= need for u in units),
        )  # fmt: skip

    # ---------- tasks ----------

    async def next(self, actor: Subject, queue_ref: str) -> ReviewTask | None:
        """The reviewer's current task, or the next one with a free slot; None when none is left."""
        await self._evals.check_on(actor)
        run_key, queue_key = _split(queue_ref, 2)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            run, _ = await self._evals.run_access(session, actor, run_key, "review")
            row = await store.queue_by_key(session, run_id=run.id, key=queue_key, lock=True)
            if row is None:
                raise EvalNotFound("No such queue or task here.")
            queue = ReviewQueue.model_validate(row.document)
            if queue.status == "closed" or not self._may_review(actor, queue):
                raise EvalForbidden("This queue is closed or not yours to review.")
            index = await store.item_index(session, run_id=run.id)
            assigned = await store.assignments_of(session, queue_id=row.id)
            need = queue.reviewers_per_item or 1
            mine = {a.item_key: a for a in assigned if a.reviewer_key == actor.key}
            taken: dict[str, int] = defaultdict(int)
            for a in assigned:
                live = a.reserved_until is not None and a.reserved_until > now
                taken[a.item_key] += a.done_at is not None or (live and a.reviewer_key != actor.key)
            chosen: _Unit | None = None
            units = _units(queue.layout, queue.arms, index)
            for unit in units:
                held = mine.get(unit.key)
                if (
                    held
                    and held.done_at is None
                    and held.reserved_until
                    and held.reserved_until > now
                ):
                    chosen = unit
                    break
            for unit in [] if chosen else units:
                if unit.key not in mine and taken[unit.key] < need:
                    chosen = unit
                    break
            if chosen is None:
                return None
            until = now + LEASE
            await store.reserve_item(
                session, tenant_id=actor.tenant_id, queue_id=row.id, item_key=chosen.key,
                reviewer_key=actor.key, until=until,
            )  # fmt: skip
            done_by_me = sum(1 for a in mine.values() if a.done_at is not None)
            return await self._task(
                session,
                actor,
                run,
                row,
                queue,
                chosen,
                until=until,
                progress=(done_by_me, len(units)),
            )

    async def _columns(
        self, session: AsyncSession, run: store.RunRow, row: store.QueueRow, queue: ReviewQueue,
        unit: _Unit, reviewer: str,
    ) -> list[tuple[str, RunItem, Arm]]:  # fmt: skip
        """The unit's items as displayed: (label, item, arm), in the reviewer's order."""
        shown = Run.model_validate(run.document)
        rows = await store.items_by_key(session, run_id=run.id, keys=unit.items)
        items = sorted(
            (RunItem.model_validate(rows[k].document) for k in unit.items if k in rows),
            key=lambda i: i.arm_id,
        )
        arms = {a.id: a for a in shown.arms}
        salt = _ref(run.key, row.key)
        labels = neutral_labels([a.id for a in shown.arms], salt, shown.blind_labels)
        if queue.blind:
            _roll(salt, unit.key, reviewer).shuffle(items)
            return [(labels[i.arm_id], i, arms[i.arm_id]) for i in items]
        return [(chr(65 + n), i, arms[i.arm_id]) for n, i in enumerate(items)]

    async def _task(
        self, session: AsyncSession, actor: Subject, run: store.RunRow, row: store.QueueRow,
        queue: ReviewQueue, unit: _Unit, *, until: datetime | None, progress: tuple[int, int],
    ) -> ReviewTask:  # fmt: skip
        shown = Run.model_validate(run.document)
        columns = await self._columns(session, run, row, queue, unit, actor.key)
        case = await self._case(session, run, unit.case_key)
        salt = _ref(run.key, row.key)
        names = arm_names(shown)
        out: list[TaskColumn] = []
        for label, item, arm in columns:
            if queue.blind:
                view = blind_item_view(
                    item, label=label, token=blind_handle(salt, item.id), names=names
                )
                out.append(TaskColumn(label=label, arm=None, item=RunItem.model_validate(view)))
            else:
                out.append(TaskColumn(label=label, arm=arm, item=item))
        view = _case_view(case, names if queue.blind else None)
        comments = await self._task_comments(
            session, run, [i for _, i, _ in columns], salt, queue.blind, names
        )
        return ReviewTask(
            id=f"{_ref(run.key, row.key)}{_SEP}{unit.key}", queue=_ref(run.key, row.key),
            layout=queue.layout, blind=queue.blind, case=view,
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
        self, session: AsyncSession, run: store.RunRow, items: Sequence[RunItem], salt: str,
        blind: bool, names: frozenset[str],
    ) -> list[Comment]:  # fmt: skip
        ids = {i.id for i in items}
        found: list[Comment] = []
        for row in await store.comments_of_run(session, run_id=run.id):
            comment = Comment.model_validate(row.document)
            if comment.item_id in ids:
                found.append(_hide(comment, salt, names) if blind else comment)
        return found

    # ---------- scores ----------

    async def submit(
        self,
        actor: Subject,
        task_id: str,
        scores: Sequence[ScoreDraft],
        *,
        by: Literal["human", "agent"] = "human",
    ) -> None:
        """Record a reviewer's scores for their task, and close it for them.

        Editors and owners can also adjudicate: drafts that all name `supersedes` replace
        earlier human scores and need no task of their own.
        """
        await self._evals.check_on(actor)
        run_key, queue_key, unit_key = _split(task_id, 3)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            run, role = await self._evals.run_access(session, actor, run_key, "review")
            row = await store.queue_by_key(session, run_id=run.id, key=queue_key, lock=True)
            if row is None:
                raise EvalNotFound("No such queue or task here.")
            queue = ReviewQueue.model_validate(row.document)
            if scores and all(d.supersedes for d in scores):
                await self._adjudicate(session, actor, role, run, scores, now)
                return
            unit = next(
                (
                    u
                    for u in _units(
                        queue.layout, queue.arms, await store.item_index(session, run_id=run.id)
                    )
                    if u.key == unit_key
                ),
                None,
            )
            held = [
                a
                for a in await store.assignments_of(session, queue_id=row.id)
                if a.item_key == unit_key and a.reviewer_key == actor.key
            ]
            if unit is None or not held or held[0].done_at is not None:
                raise EvalForbidden(
                    "This task is not reserved for you, or you already finished it."
                )
            columns = await self._columns(session, run, row, queue, unit, actor.key)
            case = await self._case(session, run, unit.case_key)
            specs = {c.id: c for c in _criteria(queue, case, len(columns))}
            made = [
                self._score(actor, by, queue, d, specs, columns, now, _ref(run.key, row.key))
                for d in scores
            ]
            await store.put_scores(session, tenant_id=actor.tenant_id, run_id=run.id, scores=made)
            await store.finish_item(
                session, queue_id=row.id, item_key=unit_key, reviewer_key=actor.key, at=now
            )

    def _score(
        self,
        actor: Subject,
        by: Literal["human", "agent"],
        queue: ReviewQueue,
        draft: ScoreDraft,
        specs: Mapping[str, Criterion],
        columns: Sequence[tuple[str, RunItem, Arm]],
        now: datetime,
        salt: str,
    ) -> JsonObject:
        spec = specs.get(draft.criterion)
        if spec is None or draft.supersedes:
            raise EvalRefused(f"This task does not ask for {draft.criterion}.")
        if problem := _check_value(spec, draft.value):
            raise EvalRefused(f"{draft.criterion}: {problem}")
        by_label = {label: item for label, item, _ in columns}
        item_id: str | None = None
        pair: ItemPair | None = None
        if spec.kind == "preference":
            pair = ItemPair(a=columns[0][1].id, b=columns[1][1].id)
        else:
            target = draft.target or (columns[0][0] if len(columns) == 1 else None)
            if target not in by_label:
                raise EvalRefused(f"{draft.criterion}: name the column (A, B...) it is about.")
            item_id = by_label[target].id
        handles = {blind_handle(salt, i.id): i.id for _, i, _ in columns}
        anchors = [self._anchor(a, by_label, handles) for a in draft.anchors or []]
        score = Score(
            id=f"h-{uuid.uuid4().hex}", item_id=item_id, criterion=draft.criterion,
            pair=pair, grader=Grader(kind=by, who=PrincipalRef(kind=_WHO[by], id=actor.key)),
            value=draft.value, rationale=draft.rationale, anchors=anchors or None,
            blind=queue.blind, at=now,
        )  # fmt: skip
        return to_document(score)

    def _anchor(
        self, anchor: Anchor, by_label: Mapping[str, RunItem], handles: Mapping[str, str]
    ) -> Anchor:
        found = by_label.get(anchor.item_id) or None
        real = found.id if found else handles.get(anchor.item_id)
        if real is None:
            raise EvalRefused("An anchor names a column that is not in this task.")
        return anchor.model_copy(update={"item_id": real})

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
        """Criteria on which reviewers of one task gave different answers, for adjudication."""
        await self._evals.check_on(actor)
        run_key, queue_key = _split(queue_ref, 2)
        async with self._sessions() as session:
            run, _ = await self._evals.run_access(session, actor, run_key, "edit")
            row = await store.queue_by_key(session, run_id=run.id, key=queue_key)
            if row is None:
                raise EvalNotFound("No such queue or task here.")
            queue = ReviewQueue.model_validate(row.document)
            index = {i.key: i for i in await store.item_index(session, run_id=run.id)}
            units = _units(queue.layout, queue.arms, list(index.values()))
            raw = await store.scores_of_run(session, run_id=run.id)
            replaced = {str(d["supersedes"]) for d in raw if d.get("supersedes")}
            humans = [d for d in raw if _kind(d) == "human" and d["id"] not in replaced]
            found: list[Disagreement] = []
            for unit in units:
                keys = set(unit.items)
                groups: dict[str, list[JsonObject]] = defaultdict(list)
                for d in humans:
                    pair = d.get("pair")
                    on = d.get("item_id") in keys or (
                        isinstance(pair, dict) and pair.get("a") in keys
                    )
                    if on:
                        groups[f"{d['criterion']}|{d.get('item_id') or ''}"].append(d)
                for group in groups.values():
                    values = {
                        repr(sorted(_value(d).items()))
                        for d in group
                        if not _value(d).get("unknown")
                    }
                    if len(values) > 1:
                        found.append(
                            Disagreement(
                                task_id=f"{_ref(run.key, row.key)}{_SEP}{unit.key}",
                                criterion=str(group[0]["criterion"]),
                                target=None,
                                scores=[_hide_score(d) for d in group],
                            )
                        )
            return found

    # ---------- comments ----------

    async def comment(self, actor: Subject, draft: CommentDraft) -> Comment:
        """Add a comment on an item or a case, or from inside a review task by column."""
        await self._evals.check_on(actor)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            if draft.task_id:
                run_key, queue_key, unit_key = _split(draft.task_id, 3)
            elif draft.run_id:
                run_key, queue_key, unit_key = draft.run_id, "", ""
            else:
                raise EvalRefused("Name a task or a run to comment on.")
            run, _ = await self._evals.run_access(session, actor, run_key, "review")
            item_id = draft.item_id
            anchors = draft.anchors
            if draft.task_id:
                row = await store.queue_by_key(session, run_id=run.id, key=queue_key)
                if row is None:
                    raise EvalNotFound("No such queue or task here.")
                queue = ReviewQueue.model_validate(row.document)
                unit = next(
                    (
                        u
                        for u in _units(
                            queue.layout, queue.arms, await store.item_index(session, run_id=run.id)
                        )
                        if u.key == unit_key
                    ),
                    None,
                )
                if unit is None:
                    raise EvalNotFound("No such queue or task here.")
                columns = await self._columns(session, run, row, queue, unit, actor.key)
                by_label = {label: item for label, item, _ in columns}
                if draft.target is not None:
                    if draft.target not in by_label:
                        raise EvalRefused("Name a column of this task.")
                    item_id = by_label[draft.target].id
                handles = {blind_handle(_ref(run.key, row.key), i.id): i.id for _, i, _ in columns}
                anchors = [self._anchor(a, by_label, handles) for a in draft.anchors or []] or None
            if item_id is None and draft.case_id is None:
                raise EvalRefused("A comment is on an item or a case.")
            comment = Comment(
                id=f"c-{uuid.uuid4().hex}", item_id=item_id, case_id=draft.case_id,
                parent_id=draft.parent_id, author=PrincipalRef(kind="person", id=actor.key),
                text=draft.text, anchors=anchors, at=now,
            )  # fmt: skip
            await store.add_comment(
                session,
                tenant_id=actor.tenant_id,
                run_id=run.id,
                suite_id=run.suite_id,
                document=to_document(comment),
            )
        return comment

    async def comments(self, actor: Subject, run_id: str) -> list[Comment]:
        """Every comment on a run; for a blind reviewer they come with their task instead."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            run, role = await self._evals.run_access(session, actor, run_id, "view")
            if await self._evals.blind_required(session, actor, role, run):
                raise EvalForbidden("In a blind queue, comments come with the review task.")
            rows = await store.comments_of_run(session, run_id=run.id)
        return [Comment.model_validate(r.document) for r in rows]


def _case_view(case: Case, names: frozenset[str] | None) -> CaseView:
    view = CaseView(
        id=case.id, title=case.title, platform=case.surface.platform, history=case.history,
        turn=case.turn.messages, reference=case.expect.reference,
    )  # fmt: skip
    if names is None:
        return view
    masked = mask_document(view.model_dump(mode="json", by_alias=True), names)
    return CaseView.model_validate(masked)


def _hide(comment: Comment, salt: str, names: frozenset[str]) -> Comment:
    """A comment for a blind reader: item ids as handles, no model names."""
    update: dict[str, object] = {}
    if comment.item_id:
        update["item_id"] = blind_handle(salt, comment.item_id)
    if comment.anchors:
        update["anchors"] = [
            a.model_copy(update={"item_id": blind_handle(salt, a.item_id)}) for a in comment.anchors
        ]
    masked = mask_document(to_document(comment.model_copy(update=update)), names)
    return Comment.model_validate(masked)


def _kind(document: JsonObject) -> str:
    grader = document.get("grader")
    return str(grader.get("kind")) if isinstance(grader, dict) else ""


def _value(document: JsonObject) -> JsonObject:
    value = document.get("value")
    return value if isinstance(value, dict) else {}


def _hide_score(document: JsonObject) -> JsonObject:
    """A score without the item ids that could name an arm."""
    return {k: v for k, v in document.items() if k not in ("item_id", "pair", "anchors")}

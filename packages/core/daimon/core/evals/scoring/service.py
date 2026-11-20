"""Results of a run: summary, comparison and judging, for every adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

from daimon.core.config import Settings
from daimon.core.evals.access import Subject
from daimon.core.evals.compare import Comparison, build_comparison
from daimon.core.evals.format import (
    BlobRef,
    Case,
    Estimate,
    EventStream,
    JudgeConfig,
    ModelChoice,
    Run,
    RunItem,
    Score,
    Suite,
    ToolAlias,
    TranscriptEvent,
    to_document,
)
from daimon.core.evals.scoring.client import JudgeUnavailable
from daimon.core.evals.scoring.judges import (
    JudgeBatch,
    JudgeClient,
    JudgeDeps,
    JudgeMeter,
    JudgeUnpriced,
    estimate,
    judge,
    plan_calls,
)
from daimon.core.evals.scoring.summary import RunSummary, summarize
from daimon.core.evals.service import EvalForbidden, EvalRefused, EvalService
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_EVENT: Final = TypeAdapter(TranscriptEvent)


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class JudgeRequest(_Out):
    id: str = "judge"
    kind: Literal["rubric", "pairwise", "reference"] = "rubric"
    models: list[ModelChoice] | None = None
    """None: the panel in settings."""
    aggregate: Literal["majority", "mean", "all"] = "majority"
    batch: bool = False
    """Send the calls through the provider's batch API at its discount, when it has one."""
    approve_cap_usd: float | None = Field(default=None, gt=0)
    """Set: the person approves the estimate and this spending ceiling, and judging runs."""


class JudgeOutcome(_Out):
    estimate: Estimate
    ran: bool
    scores: int = 0
    spent_usd: float = 0.0
    skipped: int = 0
    """Calls left undone because the ceiling was reached."""


@dataclass(frozen=True)
class _Loaded:
    run_row: store.RunRow
    run: Run
    suite: Suite | None
    items: list[RunItem]
    cases: list[Case]
    scores: list[Score]


class _NoClient:
    async def complete(self, *_: object) -> object:
        raise JudgeUnavailable("Judging is not set up here.")


async def with_transcript(
    item: RunItem, fetch: Callable[[str], Awaitable[bytes | None]]
) -> RunItem:
    """The item with its transcript read from the blob when it is stored there (NDJSON)."""
    stream = item.transcript
    if stream is None or stream.events is not None or stream.blob is None:
        return item
    data = await fetch(stream.blob.sha256)
    if data is None:
        return item
    events = [_EVENT.validate_json(line) for line in data.splitlines() if line.strip()]
    return item.model_copy(update={"transcript": EventStream(events=events, blob=stream.blob)})


class ScoringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        client: JudgeClient | None = None,
        batch: JudgeBatch | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._evals = EvalService(sessions, settings)
        self._client = client
        self._batch = batch
        self._clock = clock

    async def _load(
        self, session: AsyncSession, actor: Subject, run_id: str, action: Literal["view", "edit"]
    ) -> tuple[_Loaded, bool]:
        """The run with what scoring needs, and whether the reader must see it blind."""
        row, role = await self._evals.run_access(session, actor, run_id, action)
        blind = await self._evals.blind_required(session, actor, role, row)
        run = Run.model_validate(row.document)
        document = await store.suite_document(
            session, suite_id=row.suite_id, version=row.suite_version
        )
        cases = [
            Case.model_validate(c.document)
            for c in await store.version_cases(
                session, suite_id=row.suite_id, version=row.suite_version
            )
        ]
        items = [
            RunItem.model_validate(r.document)
            for r in await store.all_items(session, run_id=row.id)
        ]
        scores = [
            Score.model_validate(d) for d in await store.scores_of_run(session, run_id=row.id)
        ]
        loaded = _Loaded(
            run_row=row, run=run, suite=Suite.model_validate(document) if document else None,
            items=items, cases=cases, scores=scores,
        )  # fmt: skip
        return loaded, blind

    async def summary(self, actor: Subject, run_id: str) -> RunSummary:
        """Per-arm results, paired comparisons, preferences and judge agreement."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            found, blind = await self._load(session, actor, run_id, "view")
        if blind:
            raise EvalForbidden("Results stay hidden while you review this run blind.")
        rule = found.suite.defaults.stats if found.suite and found.suite.defaults else None
        return summarize(
            found.run, found.items, found.scores, found.cases, rule=rule,
            kappa_warn=self._settings.evals.kappa_warn,
        )  # fmt: skip

    async def compare(
        self, actor: Subject, run_id: str, *, arms: Sequence[str] | None = None, blind: bool = False
    ) -> Comparison:
        """The arms side by side, case by case; blind when asked or when the reader must be."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            found, forced = await self._load(session, actor, run_id, "view")
        return build_comparison(
            found.run, found.items, found.scores, found.cases, blind=blind or forced, arm_ids=arms
        )

    def _config(self, request: JudgeRequest) -> JudgeConfig:
        models = request.models or [
            ModelChoice(backend=m.backend, model=m.model, effort=m.effort)
            for m in self._settings.evals.judge_models
        ]
        if not models:
            raise EvalRefused("No judge models are set. Name some, or set the default panel.")
        return JudgeConfig(
            id=request.id, kind=request.kind, models=models, aggregate=request.aggregate
        )

    def _deps(self, tenant: Subject, request: JudgeRequest) -> JudgeDeps:
        async def load_text(ref: BlobRef) -> str | None:
            data = await self._blob(tenant, ref.sha256)
            return None if data is None else data.decode("utf-8", errors="replace")

        return JudgeDeps(
            client=self._client or _NoClient(),  # pyright: ignore[reportArgumentType]
            load_text=load_text,
            batch=self._batch if request.batch else None,
            now=self._clock,
        )

    async def _blob(self, actor: Subject, sha256: str) -> bytes | None:
        async with self._sessions() as session:
            return await store.blob_content(session, tenant_id=actor.tenant_id, sha256=sha256)

    async def judge(self, actor: Subject, run_id: str, request: JudgeRequest) -> JudgeOutcome:
        """Estimate judging a run; with `approve_cap_usd`, run it and keep the scores.

        Editors only. Judging is paid: it never runs without an approved ceiling, and stops
        when the ceiling is reached. Running again replaces the same scores.
        """
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            found, _ = await self._load(session, actor, run_id, "edit")
        config = self._config(request)
        deps = self._deps(actor, request)
        cases = {c.id: c for c in found.cases}
        aliases: list[ToolAlias] = (found.suite.tool_aliases or []) if found.suite else []
        items = [
            await with_transcript(i, lambda sha: self._blob(actor, sha))
            for i in found.items
            if i.status == "done"
        ]
        try:
            planned = await plan_calls(deps, found.run, config, items, cases, aliases)
            guess = estimate(planned, batch=deps.batch is not None, at=self._clock())
            if request.approve_cap_usd is None:
                return JudgeOutcome(estimate=guess, ran=False)
            meter = JudgeMeter(cap_usd=request.approve_cap_usd)
            scores = await judge(
                deps, found.run, config, items, cases, aliases=aliases, meter=meter
            )
        except (JudgeUnpriced, JudgeUnavailable) as exc:
            raise EvalRefused(str(exc)) from None
        async with self._sessions() as session, session.begin():
            await store.put_scores(
                session, tenant_id=actor.tenant_id, run_id=found.run_row.id,
                scores=[to_document(s) for s in scores],
            )  # fmt: skip
        return JudgeOutcome(
            estimate=guess, ran=True, scores=len(scores), spent_usd=meter.spent_usd,
            skipped=meter.skipped,
        )  # fmt: skip

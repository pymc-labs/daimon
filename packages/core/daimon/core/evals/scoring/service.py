"""Results of a run: summary, comparison and judging, for every adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Literal

from daimon.core.config import Settings
from daimon.core.evals.access import Subject, SuiteRole
from daimon.core.evals.blind import is_blind
from daimon.core.evals.compare import Comparison, build_comparison
from daimon.core.evals.format import (
    Approval,
    BlobRef,
    Case,
    Estimate,
    JsonObject,
    JudgeConfig,
    ModelChoice,
    PrincipalRef,
    Run,
    RunItem,
    Score,
    Suite,
    ToolAlias,
    to_document,
)
from daimon.core.evals.scoring.checks import check_item
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
from daimon.core.evals.service import EvalForbidden, EvalRefused, EvalService, with_transcript
from daimon.core.evals.spend import add_judge_approval, add_spend
from daimon.core.stores import evals as store
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
    """Calls left undone because the ceiling had no room for them."""
    failed: int = 0
    """Calls that failed; the scores of the others are kept."""


@dataclass(frozen=True)
class _Loaded:
    run_row: store.RunRow
    role: SuiteRole
    run: Run
    suite: Suite | None
    items: list[RunItem]
    """With their transcripts read from blobs."""
    secret: str
    """The workspace's secret behind every blind handle, label and shuffle."""
    cases: list[Case]
    scores: list[Score]
    """Stored scores, and the code checks of items that have none stored."""


class _NoClient:
    async def complete(self, *_: object, **__: object) -> object:
        raise JudgeUnavailable("Judging is not set up here.")


def _clashes(models: Sequence[ModelChoice], run: Run) -> list[str]:
    """Judge models that share a model or a provider with a live arm of the run."""
    arms = [a.live for a in run.arms if a.live is not None]
    return [
        m.model for m in models
        if any(
            m.model == a.model or (m.backend or DEFAULT_BACKEND) == (a.backend or DEFAULT_BACKEND)
            for a in arms
        )
    ]  # fmt: skip


def _code_scores(case: Case, item: RunItem, aliases: Sequence[ToolAlias]) -> list[Score]:
    """The item's code checks; none while its transcript blob could not be read."""
    stream = item.transcript
    if stream is not None and stream.events is None and stream.blob is not None:
        return []
    return check_item(case, item, aliases, at=item.finished_at)


def _new_scores(
    items: Sequence[RunItem],
    cases: Mapping[str, Case],
    aliases: Sequence[ToolAlias],
    known: set[str],
) -> list[Score]:
    """Code scores not stored yet. Run in a thread: a pattern may take its whole time limit."""
    return [
        score
        for item in items
        if (case := cases.get(item.case.id)) is not None
        for score in _code_scores(case, item, aliases)
        if score.id not in known
    ]  # fmt: skip


class ScoringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        evals: EvalService,
        *,
        client: JudgeClient | None = None,
        batch: JudgeBatch | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._evals = evals
        self._client = client
        self._batch = batch
        self._clock = clock

    async def _load(self, actor: Subject, run_id: str, action: Literal["view", "edit"]) -> _Loaded:
        """The run with what scoring needs: items with transcripts, and code checks."""
        async with self._sessions() as session, session.begin():
            secret = await store.instance_secret(session, tenant_id=actor.tenant_id)
            row, role = await self._evals.run_access(session, actor, run_id, action)
            document = await store.suite_document(
                session, suite_id=row.suite_id, version=row.suite_version
            )
            cases = [
                Case.model_validate(c.document)
                for c in await store.version_cases(
                    session, suite_id=row.suite_id, version=row.suite_version
                )
            ]
            item_rows = await store.all_items(session, run_id=row.id)
            stored = [
                Score.model_validate(d) for d in await store.scores_of_run(session, run_id=row.id)
            ]
        suite = Suite.model_validate(document) if document else None
        aliases: list[ToolAlias] = (suite.tool_aliases or []) if suite else []
        fetch = partial(self._evals.blob_bytes, actor.tenant_id)
        items = [
            await with_transcript(RunItem.model_validate(r.document), fetch) for r in item_rows
        ]
        by_case = {c.id: c for c in cases}
        known = {s.id for s in stored}
        computed = await asyncio.to_thread(_new_scores, items, by_case, aliases, known)
        return _Loaded(
            run_row=row, role=role, run=Run.model_validate(row.document), suite=suite,
            items=items, secret=secret, cases=cases,
            scores=stored + computed,
        )  # fmt: skip

    async def summary(self, actor: Subject, run_id: str) -> RunSummary:
        """Per-arm results, paired comparisons, preferences and judge agreement."""
        await self._evals.check_on(actor)
        found = await self._load(actor, run_id, "view")
        if is_blind(found.role):
            raise EvalForbidden("Results stay hidden while you review this run blind.")
        rule = found.suite.defaults.stats if found.suite and found.suite.defaults else None
        return summarize(
            found.run, found.items, found.scores, found.cases, rule=rule,
            kappa_warn=self._settings.evals.kappa_warn,
        )  # fmt: skip

    async def compare(
        self, actor: Subject, run_id: str, *, arms: Sequence[str] | None = None, blind: bool = False
    ) -> Comparison:
        """The arms side by side, case by case. Reviewers, and `blind`, get it blind: `arms`
        then names neutral labels, and each column's `item_id` opens the blind item view."""
        await self._evals.check_on(actor)
        found = await self._load(actor, run_id, "view")
        return build_comparison(
            found.run, found.items, found.scores, found.cases, blind=is_blind(found.role, blind),
            secret=found.secret, arms=arms,
        )  # fmt: skip

    def _config(self, request: JudgeRequest, run: Run) -> JudgeConfig:
        models = request.models or [
            ModelChoice(backend=m.backend, model=m.model, effort=m.effort)
            for m in self._settings.evals.judge_models
        ]
        if not models:
            raise EvalRefused("Name the judge models, or ask an admin to set the default panel.")
        if clash := _clashes(models, run):
            raise EvalRefused(
                f"A judge can't share a model or a provider with an arm: {', '.join(clash)}."
            )
        return JudgeConfig(
            id=request.id, kind=request.kind, models=models, aggregate=request.aggregate
        )

    def _deps(self, actor: Subject, request: JudgeRequest) -> JudgeDeps:
        async def load_text(ref: BlobRef) -> str | None:
            data = await self._evals.blob_bytes(actor.tenant_id, ref.sha256)
            return None if data is None else data.decode("utf-8", errors="replace")

        return JudgeDeps(
            client=self._client or _NoClient(),  # pyright: ignore[reportArgumentType]
            load_text=load_text,
            tenant_id=str(actor.tenant_id),
            batch=self._batch if request.batch else None,
            now=self._clock,
        )

    async def judge(self, actor: Subject, run_id: str, request: JudgeRequest) -> JudgeOutcome:
        """Estimate judging a run; with `approve_cap_usd`, run it and keep the scores.

        Editors only; a ceiling above the admin limit needs an admin. Judging never runs
        without an approved ceiling and never spends past it. The scores made, the spend and
        the approval are kept even when some calls fail; judging again replaces this judge's
        scores, and drops the votes of models no longer on its panel.
        """
        await self._evals.check_on(actor)
        cap = request.approve_cap_usd
        limit = self._settings.evals.admin_approval_usd
        if cap is not None and cap > limit and not actor.is_admin:
            raise EvalForbidden(
                f"Ceilings above ${limit:g} need an admin. Ask an admin or an owner."
            )
        found = await self._load(actor, run_id, "edit")
        config = self._config(request, found.run)
        deps = self._deps(actor, request)
        cases = {c.id: c for c in found.cases}
        aliases: list[ToolAlias] = (found.suite.tool_aliases or []) if found.suite else []
        items = [i for i in found.items if i.status == "done"]
        try:
            planned = await plan_calls(deps, found.run, config, items, cases, aliases)
            guess = estimate(planned, batch=deps.batch is not None, at=self._clock())
            if cap is None:
                return JudgeOutcome(estimate=guess, ran=False)
            meter = JudgeMeter(cap_usd=cap)
            try:
                scores = await judge(
                    deps, found.run, config, items, cases, aliases=aliases, meter=meter
                )
            finally:
                if meter.spent_usd:
                    await self._record(actor, found, config, meter, cap)
        except (JudgeUnpriced, JudgeUnavailable) as exc:
            raise EvalRefused(str(exc)) from None
        models = {m.model for m in config.models}
        stale = [
            s.id for s in found.scores
            if s.grader.kind == "llm" and s.grader.judge_id == config.id
            and s.grader.model not in models
        ]  # fmt: skip
        async with self._sessions() as session, session.begin():
            await store.delete_scores(session, run_id=found.run_row.id, keys=stale)
            await store.put_scores(
                session, tenant_id=actor.tenant_id, run_id=found.run_row.id,
                scores=[to_document(s) for s in scores],
            )  # fmt: skip
        return JudgeOutcome(
            estimate=guess, ran=True, scores=len(scores), spent_usd=meter.spent_usd,
            skipped=meter.skipped, failed=meter.failed,
        )  # fmt: skip

    async def _record(
        self, actor: Subject, found: _Loaded, config: JudgeConfig, meter: JudgeMeter, cap: float
    ) -> None:
        """Keep on the run what judging spent, who approved which ceiling, and the judge."""
        approval = Approval(
            by=PrincipalRef(kind="person", id=actor.key), at=self._clock(), cap_usd=cap
        )

        def change(document: JsonObject) -> JsonObject:
            run = add_spend(Run.model_validate(document), "judge", meter.spent_usd)
            run = add_judge_approval(run, config.id, approval, meter.spent_usd)
            judges = [j for j in run.judges or [] if j.id != config.id] + [config]
            return to_document(run.model_copy(update={"judges": judges}))

        async with self._sessions() as session, session.begin():
            await store.update_run(session, run_id=found.run_row.id, change=change)
